import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import workbuddy_auth as auth
import dayuan


class CloudAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.flow = auth.CloudAuth(Path(self.tmp.name))
        self.calls = []
        self.responses = []
        self.flow.http = self.http

    def http(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def ready(self):
        self.flow.save({'phase': 'ready', 'auth': {'accessToken': 'account-token', 'refreshToken': 'refresh-1', 'expiresAt': 9e15}, 'account': {'uid': 'u1', 'enterpriseId': 'e1'}})

    def test_first_login_poll_and_exchange_without_workbuddy_files(self):
        self.responses = [{'state':'opaque-state','authUrl':'https://www.workbuddy.cn/login?platform=workbuddy&state=opaque-state'}, auth.AuthError('PENDING_11217'), {'accessToken':'account-token','refreshToken':'refresh-1','expiresIn':7200}, {'uid':'u1','enterpriseId':'e1'}, {'access_token':'wecom-token','expires_in':7200,'alias_id':'wu1','src_tid':'wc1'}]
        self.assertEqual(self.flow.start()['phase'], 'pending_token')
        self.assertEqual(self.flow.finish()['phase'], 'pending_token')
        self.assertEqual(self.flow.finish()['phase'], 'ready')
        result = self.flow.ensure_wecom()
        self.assertEqual(result['access_token'], 'wecom-token')
        self.assertEqual(result['source'], 'workbuddy_cloud')
        self.assertTrue(result['identity'])
        self.assertEqual((self.flow.state / 'cloud-auth.json').stat().st_mode & 0o777, 0o600)
        self.assertNotIn('account-token', json.dumps(self.flow.status()))

    def test_reject_external_authorization_destination(self):
        self.responses = [{'state':'x','authUrl':'https://untrusted.example/login?state=x'}]
        with self.assertRaisesRegex(auth.AuthError, 'UNEXPECTED_LOGIN_URL'):
            self.flow.start()

    def test_login_keeps_state_and_selects_supported_wecom_flow(self):
        self.responses=[{'state':'s1','authUrl':'https://www.workbuddy.cn/login?platform=workbuddy&state=s1'}]
        query=auth.urllib.parse.parse_qs(auth.urllib.parse.urlsplit(self.flow.start()['authorization_url']).query)
        self.assertEqual(query['state'],['s1'])
        self.assertEqual(query['version'],['5.8.2'])
        self.assertEqual(query['force_login_type'],['wecom'])

    def test_token_rotation_saved_before_account_lookup(self):
        self.ready()
        self.responses = [{'accessToken':'new-account-token','refreshToken':'refresh-2','expiresIn':7200}, auth.AuthError('TRANSPORT_UNCERTAIN')]
        with self.assertRaises(auth.AuthError): self.flow.ensure_wecom(refresh_account=True)
        doc = self.flow.load()
        self.assertEqual(doc['auth']['refreshToken'], 'refresh-2')
        self.assertEqual(doc['phase'], 'pending_account')
        self.responses = [{'uid':'u1','enterpriseId':'e1'}, {'access_token':'wt','expires_in':7200,'alias_id':'wu1','src_tid':'wc1'}]
        self.flow.ensure_wecom()
        self.assertEqual(sum('/auth/token/refresh' in c[1] for c in self.calls), 1)

    def test_uncertain_refresh_never_automatically_replayed(self):
        self.ready()
        self.responses = [auth.AuthError('TRANSPORT_UNCERTAIN')]
        with self.assertRaises(auth.AuthError): self.flow.ensure_wecom(refresh_account=True)
        with self.assertRaisesRegex(auth.AuthError, 'REFRESH_OUTCOME_UNKNOWN'):
            self.flow.ensure_wecom()
        self.assertEqual(len(self.calls), 1)

    def test_refresh_cannot_change_account(self):
        self.ready()
        self.responses = [{'accessToken':'new','refreshToken':'r2','expiresIn':7200}, {'uid':'other','enterpriseId':'e1'}]
        with self.assertRaisesRegex(auth.AuthError, 'ACCOUNT_CHANGED'): self.flow.ensure_wecom(refresh_account=True)
        self.assertFalse((self.flow.state / 'credentials.json').exists())

    def test_wecom_identity_cannot_change_silently(self):
        self.ready()
        self.responses = [{'access_token':'w1','expires_in':7200,'alias_id':'wu1','src_tid':'wc1'}, {'access_token':'w2','expires_in':7200,'alias_id':'wu2','src_tid':'wc1'}]
        first = self.flow.ensure_wecom()
        with self.assertRaisesRegex(auth.AuthError, 'WECOM_IDENTITY_CHANGED'): self.flow.ensure_wecom(force=True)
        self.assertEqual(auth.load_private(self.flow.state / 'credentials.json')['access_token'], first['access_token'])

    def test_unexpired_cache_avoids_cloud_requests(self):
        self.ready()
        self.responses = [{'access_token':'w1','expires_in':7200,'alias_id':'wu1','src_tid':'wc1'}]
        self.flow.ensure_wecom()
        self.flow.ensure_wecom()
        self.assertEqual(len(self.calls), 1)

    def test_expired_account_renews_before_wecom_exchange(self):
        self.ready()
        d=self.flow.load();d['auth']['expiresAt']=1;self.flow.save(d)
        self.responses=[{'accessToken':'new','refreshToken':'r2','expiresIn':7200},{'uid':'u1','enterpriseId':'e1'},{'access_token':'w1','expires_in':7200,'alias_id':'wu1','src_tid':'wc1'}]
        self.flow.ensure_wecom()
        self.assertTrue(self.calls[0][1].endswith('/auth/token/refresh'))

    def test_endpoint_allowlist_rejects_before_network(self):
        flow = auth.CloudAuth(Path(self.tmp.name))
        with self.assertRaisesRegex(auth.AuthError, 'ENDPOINT_NOT_ALLOWED'):
            flow.http('POST', '/console/logout')

    def test_task_resumes_after_rotation_for_same_wecom_identity(self):
        self.ready()
        dayuan.save_private(self.flow.state/'requests'/'rotation-case.json', {'request_id':'rotation-case','credential_fingerprint':dayuan.digest('old'), 'credential_identity':'same-user','task_id':'task1','context_id':'ctx1','state':'working'})
        client=dayuan.Client(self.flow.state)
        with patch.object(auth.CloudAuth,'ensure_wecom',return_value={'access_token':'rotated','identity':'same-user','expires_at_ms':9e15}), patch.object(client,'request',return_value={'id':'task1','contextId':'ctx1','status':{'state':3}}):
            self.assertEqual(client.result('rotation-case')['state'],'completed')

    def test_task_rejects_new_identity_even_if_raw_token_matches(self):
        self.ready()
        dayuan.save_private(self.flow.state/'requests'/'rotation-case.json', {'request_id':'rotation-case','credential_fingerprint':dayuan.digest('same-token'), 'credential_identity':'old-user','task_id':'task1','context_id':'ctx1','state':'working'})
        client=dayuan.Client(self.flow.state)
        with patch.object(auth.CloudAuth,'ensure_wecom',return_value={'access_token':'same-token','identity':'new-user','expires_at_ms':9e15}), patch.object(client,'request') as request:
            with self.assertRaisesRegex(dayuan.Error,'CREDENTIAL_CHANGED'): client.result('rotation-case')
            request.assert_not_called()


if __name__ == '__main__': unittest.main()
