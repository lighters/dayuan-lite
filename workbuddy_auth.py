#!/usr/bin/env python3
"""Desktop-free WorkBuddy cloud authorization. No Electron, SDK or desktop files."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from dayuan import DEFAULT_STATE, Error as StateError, load_private, private_dir, save_private

ORIGIN = 'https://www.workbuddy.cn'
PREFIX = '/v2/plugin'
WECOM_LOGIN = ORIGIN + '/login/wecom-auth'
# Protocol compatibility with the analyzed desktop auth flow. The public page
# hides WeCom login when platform=workbuddy has no version or a version < 5.8.0.
AUTH_FLOW_VERSION = '5.8.2'


def login_url(url):
    parsed=urllib.parse.urlsplit(url)
    query=urllib.parse.parse_qs(parsed.query,keep_blank_values=True)
    query['version']=[AUTH_FLOW_VERSION]
    query['force_login_type']=['wecom']
    return urllib.parse.urlunsplit((parsed.scheme,parsed.netloc,parsed.path,urllib.parse.urlencode(query,doseq=True),''))


class AuthError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AuthError('REDIRECT_REJECTED')


def timestamp():
    return time.time() * 1000


def secret(value):
    if not isinstance(value, str) or not value or re.search(r'[\x00-\x20\x7f]', value):
        raise AuthError('INVALID_CREDENTIAL')
    return value


def deadline(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
        return value
    return 0


def normalize_auth(value):
    if not isinstance(value, dict): raise AuthError('INVALID_AUTH_RESPONSE')
    result = dict(value)
    secret(result.get('accessToken'))
    if result.get('refreshToken'): secret(result['refreshToken'])
    for absolute, duration in [('expiresAt','expiresIn'), ('refreshExpiresAt','refreshExpiresIn')]:
        result[absolute] = deadline(result.get(absolute)) or (timestamp() + deadline(result.get(duration)) * 1000 if deadline(result.get(duration)) else 0)
    return result


def account_identity(account):
    if not isinstance(account, dict) or not isinstance(account.get('uid'), str) or not account['uid']:
        raise AuthError('INVALID_ACCOUNT_RESPONSE')
    return account['uid'], account.get('enterpriseId')


class CloudAuth:
    def __init__(self, state=DEFAULT_STATE):
        self.state = Path(state)
        self.path = self.state / 'cloud-auth.json'

    @contextmanager
    def locked(self):
        private_dir(self.state)
        fd = os.open(str(self.state / '.auth.lock'), os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise AuthError('AUTH_BUSY') from None
            yield
        finally:
            os.close(fd)

    def load(self):
        try: return load_private(self.path)
        except FileNotFoundError: return {'phase': 'logged_out'}

    def save(self, doc):
        save_private(self.path, doc)

    def http(self, method, path, auth=None, refresh=None):
        allowed = (
            (method == 'POST' and path in [PREFIX+'/auth/state?platform=workbuddy', PREFIX+'/auth/token/refresh']) or
            (method == 'GET' and (path == PREFIX+'/account' or re.fullmatch(r'/v2/plugin/auth/token\?state=[A-Za-z0-9_%.-]+', path))) or
            (method == 'POST' and re.fullmatch(r'/console/enterprises/[A-Za-z0-9_%.-]+/oneid_openapi/exchange-wecom-token', path))
        )
        if not allowed: raise AuthError('ENDPOINT_NOT_ALLOWED')
        headers = {'Accept':'application/json', 'Content-Type':'application/json', 'X-Domain':'www.workbuddy.cn', 'X-Product':'SaaS', 'User-Agent':'dayuan-lite/0.2 (local interoperability client)'}
        if auth: headers['Authorization'] = 'Bearer ' + secret(auth['accessToken'])
        if refresh:
            headers['X-Refresh-Token'] = secret(refresh)
            headers['X-Auth-Refresh-Source'] = 'plugin'
        req = urllib.request.Request(ORIGIN+path, method=method, headers=headers, data=b'{}' if method == 'POST' else None)
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=25) as response:
                raw = response.read(1024*1024+1)
        except urllib.error.HTTPError as exc:
            # Preserve machine codes only; never reflect a server body with secrets.
            try: code = json.loads(exc.read(65536)).get('code')
            except (ValueError, AttributeError): code = None
            if code in (11217,12151): raise AuthError('PENDING_'+str(code)) from None
            raise AuthError('HTTP_'+str(exc.code)+(('_CODE_'+str(code)) if isinstance(code, int) else '')) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise AuthError('TRANSPORT_UNCERTAIN') from None
        if len(raw) > 1024*1024: raise AuthError('RESPONSE_TOO_LARGE')
        try: body = json.loads(raw)
        except ValueError: raise AuthError('INVALID_RESPONSE') from None
        if not isinstance(body, dict): raise AuthError('INVALID_RESPONSE')
        code = body.get('code')
        if code in (11217,12151): raise AuthError('PENDING_'+str(code))
        if code != 0: raise AuthError('CLOUD_CODE_'+str(code) if isinstance(code,int) else 'INVALID_RESPONSE')
        if not isinstance(body.get('data'),dict): raise AuthError('INVALID_RESPONSE')
        return body['data']

    def status(self):
        d = self.load()
        a = d.get('auth',{})
        return {'phase':d.get('phase'), 'desktop_required':False, 'cloud_dependency':ORIGIN,
                'account_expires_at_ms':a.get('expiresAt'), 'refresh_expires_at_ms':a.get('refreshExpiresAt'),
                'refresh_credential_present':bool(a.get('refreshToken')), 'wecom_expires_at_ms':d.get('wecom',{}).get('expires_at_ms'),
                'wecom_configured':bool(d.get('wecom')), 'authorization_verified':False}

    def start(self):
        with self.locked():
            d = self.load()
            if d.get('phase') == 'ready': raise AuthError('ALREADY_LOGGED_IN_USE_RENEW')
            pending = d.get('pending',{})
            if d.get('phase') == 'pending_token' and timestamp()-pending.get('created_at_ms',0) < 300000:
                pending['url']=login_url(pending['url']); self.save(d)
                return {'phase':'pending_token','authorization_url':pending['url']}
            response = self.http('POST',PREFIX+'/auth/state?platform=workbuddy')
            state, url = response.get('state'), response.get('authUrl')
            parsed = urllib.parse.urlsplit(url) if isinstance(url,str) else None
            if not parsed or (parsed.scheme,parsed.netloc,parsed.path) != ('https','www.workbuddy.cn','/login') or parsed.fragment:
                raise AuthError('UNEXPECTED_LOGIN_URL')
            if not isinstance(state,str) or not state or urllib.parse.parse_qs(parsed.query).get('state') != [state]:
                raise AuthError('INVALID_LOGIN_STATE')
            url=login_url(url)
            d.update(phase='pending_token',pending={'state':state,'url':url,'created_at_ms':timestamp()})
            self.save(d)
            return {'phase':'pending_token','authorization_url':url}

    def _account(self, d):
        account = self.http('GET',PREFIX+'/account',auth=d['auth'])
        identity = account_identity(account)
        if d.get('account') and account_identity(d['account']) != identity:
            d['phase']='account_changed'; self.save(d)
            raise AuthError('ACCOUNT_CHANGED_USE_SEPARATE_STATE_DIRECTORY')
        d.update(account=account,phase='ready')
        d.pop('pending',None)
        self.save(d)

    def finish(self):
        with self.locked():
            d = self.load()
            if d.get('phase') == 'pending_token':
                # A bounded CLI wait may end just before the user confirms.
                # A manual finish asks the server once; server expiry remains authoritative.
                try: value = self.http('GET', PREFIX+'/auth/token?state='+urllib.parse.quote(d['pending']['state'],safe=''))
                except AuthError as exc:
                    if str(exc).startswith('PENDING_'): return {'phase':'pending_token'}
                    raise
                d.update(auth=normalize_auth(value),phase='pending_account')
                self.save(d)
            if d.get('phase') == 'pending_account':
                try: self._account(d)
                except AuthError as exc:
                    if str(exc).startswith('PENDING_'): return {'phase':'pending_account'}
                    raise
            if d.get('phase') != 'ready': raise AuthError('LOGIN_REQUIRED')
            return {'phase':'ready','desktop_required':False,'cloud_dependency':ORIGIN}

    def _refresh(self,d):
        token = d.get('auth',{}).get('refreshToken')
        if not token:
            d['phase']='login_required'; self.save(d)
            raise AuthError('LOGIN_REQUIRED_NO_REFRESH_TOKEN')
        expiration = deadline(d['auth'].get('refreshExpiresAt'))
        if expiration and expiration <= timestamp():
            d['phase']='login_required'; self.save(d)
            raise AuthError('LOGIN_REQUIRED_REFRESH_EXPIRED')
        # A lost response can have rotated the credential. Never blindly replay it.
        d['phase']='refresh_unknown'; self.save(d)
        try: value = self.http('POST',PREFIX+'/auth/token/refresh',auth=d['auth'],refresh=token)
        except AuthError as exc:
            if str(exc).startswith(('HTTP_401','HTTP_403')):
                d['phase']='login_required'; self.save(d)
            raise
        d.update(auth=normalize_auth(value),phase='pending_account')
        self.save(d)
        self._account(d)

    def ensure_wecom(self, force=False, refresh_account=False):
        with self.locked():
            d = self.load()
            if d.get('phase') == 'refresh_unknown': raise AuthError('REFRESH_OUTCOME_UNKNOWN_LOGIN_REQUIRED')
            if d.get('phase') == 'pending_account': self._account(d)
            if d.get('phase') != 'ready': raise AuthError('LOGIN_REQUIRED')
            a=d['auth']
            if refresh_account or not deadline(a.get('expiresAt')) or a['expiresAt'] <= timestamp()+60000:
                self._refresh(d)
            current=d.get('wecom')
            if not force and not refresh_account and current and current.get('expires_at_ms',0)>timestamp()+60000:
                return current
            enterprise=d['account'].get('enterpriseId')
            if not isinstance(enterprise,str) or not enterprise: raise AuthError('ENTERPRISE_ACCOUNT_REQUIRED')
            route='/console/enterprises/'+urllib.parse.quote(enterprise,safe='')+'/oneid_openapi/exchange-wecom-token'
            try: value=self.http('POST',route,auth=d['auth'])
            except AuthError as exc:
                if str(exc).startswith('HTTP_403'): raise AuthError('WECOM_AUTH_REQUIRED_OPEN_WECOM_LOGIN') from None
                raise
            token=secret(value.get('access_token'))
            user,corp=value.get('alias_id'),value.get('src_tid')
            if not isinstance(user,str) or not user or not isinstance(corp,str) or not corp: raise AuthError('WECOM_STABLE_IDENTITY_MISSING')
            identity=hashlib.sha256(json.dumps([corp,user],separators=(',',':')).encode()).hexdigest()
            if current and current.get('identity') != identity: raise AuthError('WECOM_IDENTITY_CHANGED')
            duration=deadline(value.get('expires_in'))
            if not duration: raise AuthError('WECOM_EXPIRY_MISSING')
            cred={'access_token':token,'identity':identity,'expires_at_ms':timestamp()+duration*1000,'source':'workbuddy_cloud'}
            d['wecom']=cred; self.save(d)
            save_private(self.state/'credentials.json',cred)
            return cred


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir',type=Path,default=Path(os.environ.get('DAYUAN_STATE_DIR',DEFAULT_STATE)))
    parser.add_argument('command',choices=['login','finish','renew','status','wecom-login'])
    parser.add_argument('--open',action='store_true',help='Open the official authorization page in the system browser')
    parser.add_argument('--wait',type=int,default=0,help='Poll login for at most 300 seconds')
    args=parser.parse_args()
    flow=CloudAuth(args.state_dir)
    try:
        if args.command=='status': value=flow.status()
        elif args.command=='wecom-login': value={'authorization_url':WECOM_LOGIN,'instruction':'在完成账号登录的同一浏览器中，授权同一企业微信账号。之后运行 renew。'}
        elif args.command=='login': value=flow.start()
        elif args.command=='renew':
            flow.ensure_wecom(force=True,refresh_account=True); value=flow.status()
        else: value=flow.finish()
        if args.open and value.get('authorization_url'): webbrowser.open(value['authorization_url'])
        print(json.dumps(value,ensure_ascii=False),flush=True)
        if args.command in ('login','finish') and args.wait:
            end=time.monotonic()+min(max(args.wait,0),300)
            while value.get('phase')!='ready' and time.monotonic()<end:
                time.sleep(2); value=flow.finish()
            if value.get('phase')=='ready':
                flow.ensure_wecom(); value=flow.status()
            print(json.dumps(value,ensure_ascii=False),flush=True)
    except (AuthError,StateError,OSError,ValueError) as exc:
        print(json.dumps({'error':str(exc) if isinstance(exc,AuthError) else 'LOCAL_STATE_ERROR'},ensure_ascii=False)); sys.exit(1)


if __name__=='__main__': main()
