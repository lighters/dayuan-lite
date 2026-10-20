import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import dayuan
from workbuddy_auth import AuthError


class WecomBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.binary = self.root / 'wecom-cli'
        self.log = self.root / 'calls.jsonl'
        self.binary.write_text('''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
if args == ['--version']:
    with open(os.environ['FAKE_LOG'] + '.version', 'a') as f: f.write('checked\\n')
    print('wecom-cli ' + os.environ.get('FAKE_VERSION', '1.3.4'))
    sys.exit(0)
entry = {'pid':os.getpid(), 'argv':args, 'has_token':os.environ.get('WECOM_CLI_ACCESS_TOKEN') == 'fresh-test-token', 'old_headers':any(k.startswith('WECOM_CLI_ADDITIONAL_HEADERS') for k in os.environ), 'config':os.environ.get('WECOM_CLI_CONFIG_DIR'), 'node_options':os.environ.get('NODE_OPTIONS'), 'bot_file':(Path(os.environ['WECOM_CLI_CONFIG_DIR']) / 'credentials.enc').exists()}
with open(os.environ['FAKE_LOG'], 'a') as f: f.write(json.dumps(entry)+'\\n')
mode = os.environ.get('FAKE_MODE')
if mode == 'timeout': time.sleep(5)
if mode == 'large': print('x'*2200000); sys.exit(0)
if mode == 'stderr': print(os.environ.get('WECOM_CLI_ACCESS_TOKEN'), file=sys.stderr); sys.exit(1)
if mode == 'slow-exit':
    print(json.dumps({'errcode':0,'content':'late exit'}), flush=True)
    os.close(1); os.close(2)
    time.sleep(float(os.environ.get('FAKE_EXIT_DELAY', '1')))
    sys.exit(0)
if mode == 'file':
    f=Path('content.txt'); f.write_text('large document body')
    print(json.dumps({'file_path':str(f.resolve())})); sys.exit(0)
if mode == 'file-with-content':
    f=Path('content.txt'); f.write_text('full body')
    print(json.dumps({'content':'truncated…','file_path':str(f.resolve())})); sys.exit(0)
if mode == 'stderr-large':
    sys.stderr.write('log line\\n' * 20000); sys.stderr.flush()
if mode == 'outside-file':
    print(json.dumps({'file_path':'/etc/passwd'})); sys.exit(0)
if mode == 'invalid': print('not json'); sys.exit(0)
if mode == 'error': print(json.dumps({'error':{'code':640027,'message':os.environ.get('WECOM_CLI_ACCESS_TOKEN')}})); sys.exit(0)
if mode == 'expired' and len(Path(os.environ['FAKE_LOG']).read_text().splitlines()) == 1:
    print(json.dumps({'errcode':853005,'errmsg':'expired'})); sys.exit(1)
print(json.dumps({'errcode':0,'content':'test content','echo':os.environ.get('WECOM_CLI_ACCESS_TOKEN')}))
''')
        self.binary.chmod(0o700)
        self.env = patch.dict(os.environ, {'FAKE_LOG':str(self.log), 'DAYUAN_WECOM_CLI':str(self.binary), 'WECOM_CLI_ACCESS_TOKEN':'stale-inherited-token', 'WECOM_CLI_ADDITIONAL_HEADERS_X':'unsafe', 'WECOM_CLI_CONFIG_DIR':str(self.root / 'existing-bot')})
        self.env.start()
        self.addCleanup(self.env.stop)

    def bridge(self, **kwargs):
        from wecom_bridge import Bridge
        return Bridge(self.root, **kwargs)

    def test_fresh_token_and_isolated_config_are_used_for_real_child(self):
        with patch('workbuddy_auth.CloudAuth.ensure_wecom', return_value={'access_token':'fresh-test-token'}) as auth:
            value = self.bridge().read('doc-read', {'docid':'test-doc'})
        self.assertEqual(value['data']['content'], 'test content')
        self.assertNotIn('fresh-test-token', json.dumps(value))
        auth.assert_called_once_with()
        entry = json.loads(self.log.read_text())
        self.assertTrue(entry['has_token'])
        self.assertFalse(entry['old_headers'])
        self.assertFalse(entry['bot_file'])
        self.assertNotIn('fresh-test-token', json.dumps(entry['argv']))
        self.assertFalse(Path(entry['config']).exists())

    def test_writes_and_extra_cli_arguments_are_rejected_before_auth(self):
        with patch('workbuddy_auth.CloudAuth.ensure_wecom') as auth:
            for operation, parameters in [('doc-create', {}), ('doc-read', {'docid':'d','file_path':'/tmp/x'}), ('doc-search', {'keywords':['x'],'limit':True}), ('meeting-list', {'begin_time':'bad','end_time':'bad'}), ('meeting-get', {'meeting_ids':[]})]:
                with self.assertRaises(dayuan.Error):
                    self.bridge().read(operation, parameters)
        auth.assert_not_called()
        self.assertFalse(self.log.exists())

    def test_old_cli_is_rejected_before_auth(self):
        with patch.dict(os.environ, {'FAKE_VERSION':'1.1.0'}), patch('workbuddy_auth.CloudAuth.ensure_wecom') as auth:
            with self.assertRaisesRegex(dayuan.Error, 'WECOM_CLI_VERSION_UNSUPPORTED'):
                self.bridge().read('doc-read', {'docid':'d'})
        auth.assert_not_called()
        self.assertFalse(self.log.exists())

    def test_auth_failure_never_starts_business_command(self):
        with patch('workbuddy_auth.CloudAuth.ensure_wecom', side_effect=AuthError('LOGIN_REQUIRED')):
            with self.assertRaisesRegex(dayuan.Error, 'LOGIN_REQUIRED'):
                self.bridge().read('doc-read', {'docid':'d'})
        self.assertFalse(self.log.exists())

    def test_process_errors_are_bounded_and_do_not_leak_token(self):
        for mode, error in [('timeout','WECOM_CLI_TIMEOUT'), ('large','WECOM_CLI_RESPONSE_TOO_LARGE'), ('stderr','WECOM_CLI_FAILED'), ('invalid','WECOM_CLI_INVALID_RESPONSE'), ('error','WECOM_CLI_ERROR_640027')]:
            with self.subTest(mode=mode), patch.dict(os.environ, {'FAKE_MODE':mode}), patch('workbuddy_auth.CloudAuth.ensure_wecom', return_value={'access_token':'fresh-test-token'}):
                with self.assertRaises(dayuan.Error) as raised:
                    self.bridge(timeout=1).read('doc-read', {'docid':'d'})
                self.assertEqual(str(raised.exception), error)
                if mode == 'timeout':
                    pid = json.loads(self.log.read_text().splitlines()[-1])['pid']
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)

    def test_complete_output_is_kept_when_budget_ends_before_exit(self):
        from wecom_bridge import run_cli
        import time
        with patch.dict(os.environ, {'FAKE_MODE':'slow-exit', 'FAKE_LOG':str(self.log), 'WECOM_CLI_CONFIG_DIR':str(self.root)}):
            # Budget covers the output but not the lingering exit; the output must still be returned.
            code, raw = run_cli([str(self.binary), 'doc', 'contents', 'get'], dict(os.environ), str(self.root), time.monotonic() + 0.5)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(raw)['content'], 'late exit')

    def test_exit_grace_preserves_a_longer_remaining_budget(self):
        from wecom_bridge import run_cli
        import time
        with patch.dict(os.environ, {'FAKE_MODE':'slow-exit', 'FAKE_EXIT_DELAY':'2.3', 'WECOM_CLI_CONFIG_DIR':str(self.root)}):
            code, raw = run_cli([str(self.binary), 'doc', 'contents', 'get'], dict(os.environ), str(self.root), time.monotonic() + 10)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(raw)['content'], 'late exit')

    def test_rejected_user_token_is_exchanged_once_and_read_retried(self):
        with patch.dict(os.environ, {'FAKE_MODE':'expired'}), patch('workbuddy_auth.CloudAuth.ensure_wecom', return_value={'access_token':'fresh-test-token'}) as auth:
            value = self.bridge().read('doc-read', {'docid':'d'})
        self.assertEqual(value['data']['content'], 'test content')
        self.assertEqual([call.kwargs for call in auth.call_args_list], [{}, {'force':True}])
        self.assertEqual(len(self.log.read_text().splitlines()), 2)

    def test_expired_account_is_renewed_before_business_command(self):
        from workbuddy_auth import CloudAuth
        flow = CloudAuth(self.root)
        flow.save({'phase':'ready','auth':{'accessToken':'old-account','refreshToken':'refresh-test','expiresAt':1},'account':{'uid':'u','enterpriseId':'e'}})
        replies = [{'accessToken':'new-account','refreshToken':'new-refresh','expiresIn':7200}, {'uid':'u','enterpriseId':'e'}, {'access_token':'fresh-test-token','expires_in':7200,'alias_id':'wu','src_tid':'wc'}]
        with patch('workbuddy_auth.CloudAuth.http', side_effect=replies) as http:
            value = self.bridge().read('doc-read', {'docid':'d'})
        self.assertEqual(value['data']['content'], 'test content')
        self.assertTrue(json.loads(self.log.read_text())['has_token'])
        self.assertEqual([call.args[:2] for call in http.call_args_list], [('POST','/v2/plugin/auth/token/refresh'), ('GET','/v2/plugin/account'), ('POST','/console/enterprises/e/oneid_openapi/exchange-wecom-token')])
        self.assertEqual(flow.load()['auth']['refreshToken'], 'new-refresh')

    def test_file_content_is_inlined_before_temporary_directory_disappears(self):
        with patch.dict(os.environ, {'FAKE_MODE':'file'}), patch('workbuddy_auth.CloudAuth.ensure_wecom', return_value={'access_token':'fresh-test-token'}):
            value = self.bridge().read('doc-read', {'docid':'d'})
        self.assertEqual(value['data'], {'content':'large document body'})
        with patch.dict(os.environ, {'FAKE_MODE':'outside-file'}), patch('workbuddy_auth.CloudAuth.ensure_wecom', return_value={'access_token':'fresh-test-token'}):
            with self.assertRaisesRegex(dayuan.Error, 'WECOM_CLI_INVALID_FILE'):
                self.bridge().read('doc-read', {'docid':'d'})

    def version_checks(self):
        return len(Path(str(self.log) + '.version').read_text().splitlines())

    def test_version_is_checked_once_per_binary_and_again_after_reinstall(self):
        with patch('workbuddy_auth.CloudAuth.ensure_wecom', return_value={'access_token':'fresh-test-token'}):
            self.bridge().read('doc-read', {'docid':'d'})
            self.bridge().read('doc-read', {'docid':'d'})
            self.assertEqual(self.version_checks(), 1)
            os.utime(self.binary, ns=(0, 0))
            self.bridge().read('doc-read', {'docid':'d'})
        self.assertEqual(self.version_checks(), 2)

    def test_noisy_stderr_does_not_fail_the_read_and_is_not_returned(self):
        with patch.dict(os.environ, {'FAKE_MODE':'stderr-large'}), patch('workbuddy_auth.CloudAuth.ensure_wecom', return_value={'access_token':'fresh-test-token'}):
            value = self.bridge().read('doc-read', {'docid':'d'})
        self.assertEqual(value['data']['content'], 'test content')
        self.assertNotIn('log line', json.dumps(value))

    def test_node_injection_variables_are_not_inherited(self):
        with patch.dict(os.environ, {'NODE_OPTIONS':'--require /tmp/evil.js'}), patch('workbuddy_auth.CloudAuth.ensure_wecom', return_value={'access_token':'fresh-test-token'}):
            self.bridge().read('doc-read', {'docid':'d'})
        self.assertIsNone(json.loads(self.log.read_text().splitlines()[-1])['node_options'])

    def test_inline_file_does_not_clobber_existing_content(self):
        with patch.dict(os.environ, {'FAKE_MODE':'file-with-content'}), patch('workbuddy_auth.CloudAuth.ensure_wecom', return_value={'access_token':'fresh-test-token'}):
            value = self.bridge().read('doc-read', {'docid':'d'})
        self.assertEqual(value['data'], {'content':'truncated…', 'file_content':'full body'})

    def test_status_reports_cli_presence_without_starting_it(self):
        self.assertEqual(dayuan.Client(self.root, token='t').status()['wecom_cli'], {'executable':str(self.binary), 'found':True})
        with patch.dict(os.environ, {'DAYUAN_WECOM_CLI':str(self.root / 'missing')}):
            self.assertFalse(dayuan.Client(self.root, token='t').status()['wecom_cli']['found'])
        self.assertFalse(self.log.exists())

    def test_each_operation_uses_only_its_fixed_cli_command(self):
        cases = [('doc-search', {'keywords':['test']}, ['doc','search']), ('doc-read', {'docid':'d'}, ['doc','contents','get']), ('meeting-list', {'begin_time':'2026-10-15 00:00:00','end_time':'2026-10-15 23:59:59'}, ['meeting','list']), ('meeting-get', {'meeting_ids':[{'meeting_id':'m'}]}, ['meeting','get'])]
        with patch('workbuddy_auth.CloudAuth.ensure_wecom', return_value={'access_token':'fresh-test-token'}):
            for operation, value, command in cases:
                self.bridge().read(operation, value)
                argv = json.loads(self.log.read_text().splitlines()[-1])['argv']
                self.assertEqual(argv[:-2], command)
                self.assertEqual(argv[-2], '--json')
                self.assertEqual(json.loads(argv[-1]).get('limit'), 10 if operation in ('doc-search','meeting-list') else None)

    def test_cli_and_mcp_dispatch_to_same_read_only_bridge(self):
        with patch('workbuddy_auth.CloudAuth.ensure_wecom', return_value={'access_token':'fresh-test-token'}), patch('sys.stdout', new_callable=io.StringIO) as output:
            dayuan.main(['--state-dir',str(self.root),'wecom','doc-read','--json','{"docid":"d"}'])
        self.assertEqual(json.loads(output.getvalue())['data']['content'], 'test content')
        msg={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'dayuan_wecom_read','arguments':{'operation':'doc-read','parameters':{'docid':'d'}}}}
        with patch.dict(os.environ, {'DAYUAN_ACCESS_TOKEN':'invalid\ninherited'}), patch('workbuddy_auth.CloudAuth.ensure_wecom', return_value={'access_token':'fresh-test-token'}), patch('sys.stdin', io.StringIO(json.dumps(msg)+'\n')), patch('sys.stdout', new_callable=io.StringIO) as output:
            dayuan.serve(self.root)
        reply=json.loads(output.getvalue())
        self.assertEqual(json.loads(reply['result']['content'][0]['text'])['data']['content'], 'test content')
