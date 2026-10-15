import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import dayuan


QUERY = {"request_id": "test-query-001", "operation": "read_chat", "chat": "测试群", "since": "2026-10-15T00:00:00+08:00", "until": "2026-10-15T12:00:00+08:00", "limit": 3}
TASK = {"id": "task_test_1", "contextId": "context-1", "status": {"state": "TASK_STATE_COMPLETED"}, "history": [{"role": "ROLE_USER", "parts": [{"text": "private query"}]}, {"role": "ROLE_AGENT", "parts": [{"text": "测试摘要"}]}]}


class DayuanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.client = dayuan.Client(self.root, token="test-private-token")

    def tearDown(self):
        self.temp.cleanup()

    def test_query_rejects_writes_and_unpaired_time(self):
        for change in [{"operation": "send_message"}, {"limit": True}, {"since": "bad"}, {"until": None}, {"extra": "send"}]:
            with self.assertRaises(dayuan.Error):
                dayuan.validate_query({**QUERY, **change})

    def test_result_uses_agent_text_and_marks_provenance(self):
        result = dayuan.normalize(TASK)
        self.assertEqual(result["text"], "测试摘要")
        self.assertEqual(result["state"], "completed")
        self.assertFalse(result["raw_messages_verified"])

    def test_submit_once_and_resume_without_reposting(self):
        with patch.object(self.client, "request", return_value=TASK) as request:
            first = self.client.start(QUERY)
            second = self.client.start(QUERY)
            self.assertEqual(first["task_id"], second["task_id"])
            self.assertEqual([c.args[0] for c in request.call_args_list], ["POST", "GET"])
        record = (self.root / "requests/test-query-001.json").read_text()
        self.assertNotIn("测试群", record)
        self.assertNotIn("test-private-token", record)
        self.assertEqual((self.root / "requests/test-query-001.json").stat().st_mode & 0o777, 0o600)

    def test_uncertain_submit_cannot_be_repeated(self):
        with patch.object(self.client, "request", side_effect=dayuan.Error("TRANSPORT_UNCERTAIN")) as request:
            self.assertEqual(self.client.start(QUERY)["state"], "outcome_unknown")
            self.assertEqual(self.client.start(QUERY)["state"], "outcome_unknown")
            self.assertEqual(request.call_count, 1)

    def test_changed_query_or_identity_cannot_resume_old_task(self):
        with patch.object(self.client, "request", return_value=TASK):
            self.client.start(QUERY)
            with self.assertRaises(dayuan.Error):
                self.client.start({**QUERY, "chat": "另一个群"})
        other = dayuan.Client(self.root, token="different-account-token")
        with self.assertRaises(dayuan.Error):
            other.result(QUERY["request_id"])

    def test_mismatched_task_context_is_not_returned(self):
        with patch.object(self.client, "request", return_value=TASK):
            self.client.start(QUERY)
        with patch.object(self.client, "request", return_value={**TASK, "contextId": "other"}):
            with self.assertRaises(dayuan.Error):
                self.client.result(QUERY["request_id"])

    def test_network_destination_and_redirect_are_restricted(self):
        with self.assertRaises(dayuan.Error):
            self.client.request("GET", "https://attacker.invalid/")
        with self.assertRaises(dayuan.Error):
            self.client.request("POST", "/tasks/task_test_1:cancel")
        with self.assertRaises(dayuan.Error):
            dayuan.NoRedirect().redirect_request(None, None, 302, "", {}, "https://attacker.invalid")

    def test_import_does_not_execute_shell_and_token_stays_private(self):
        source = self.root / "export.sh"
        source.write_text("export WECOM_CLI_ACCESS_TOKEN='test-token-$(not-a-command)'\n")
        result = dayuan.import_workbuddy(self.root, source)
        self.assertNotIn("test-token", json.dumps(result))
        self.assertEqual(dayuan.Client(self.root).token, "test-token-$(not-a-command)")
        self.assertEqual((self.root / "credentials.json").stat().st_mode & 0o777, 0o600)

    def test_auth_failure_stops_and_does_not_expose_credentials(self):
        err = urllib.error.HTTPError(dayuan.BASE, 401, "test-private-token", {}, io.BytesIO(b'private response'))
        with patch("urllib.request.OpenerDirector.open", side_effect=err) as network:
            result = self.client.start(QUERY)
            self.assertEqual(result["state"], "rejected")
            self.assertEqual(result["error"], "AUTH_REQUIRED")
            self.assertEqual(network.call_count, 1)
            self.assertNotIn("test-private-token", json.dumps(result))

    def test_http_payload_and_target_are_a2a_not_a_local_proxy(self):
        response = io.BytesIO(json.dumps(TASK).encode())
        with patch("urllib.request.OpenerDirector.open", return_value=response) as network:
            result = self.client.start(QUERY)
            self.assertEqual(result["state"], "completed")
            req = network.call_args.args[0]
            self.assertEqual(req.full_url, "https://qyapi.weixin.qq.com/cgi-bin/a2a/api/message:send")
            body = json.loads(req.data)
            self.assertEqual(body["message"]["role"], "ROLE_USER")
            self.assertTrue(body["configuration"]["returnImmediately"])
            self.assertEqual(body["configuration"]["historyLength"], 0)

    def test_stdio_handshake_has_no_workbuddy_dependency(self):
        inp = '\n'.join(json.dumps(v) for v in [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}, {"jsonrpc": "2.0", "method": "notifications/initialized"}, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "dayuan_status", "arguments": {}}}]) + '\n'
        proc = subprocess.run([sys.executable, str(Path(dayuan.__file__)), "--state-dir", str(self.root), "serve"], input=inp, text=True, capture_output=True, env={**os.environ, "DAYUAN_ACCESS_TOKEN": "test-private-token"}, timeout=5)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        replies = [json.loads(x) for x in proc.stdout.splitlines()]
        self.assertEqual(len(replies), 3)
        self.assertEqual(len(replies[1]["result"]["tools"]), 3)
        self.assertNotIn("test-private-token", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
