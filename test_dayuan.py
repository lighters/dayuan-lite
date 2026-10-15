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

    def test_definitively_rejected_submit_can_reuse_request_id(self):
        with patch.object(self.client, "request", side_effect=[dayuan.Error("AUTH_REQUIRED", definitive=True), TASK, TASK]) as request:
            first = self.client.start(QUERY)
            self.assertEqual(first["state"], "rejected")
            self.assertTrue(first["retry_safe"])
            self.assertTrue(self.client.result(QUERY["request_id"])["retry_safe"])
            second = self.client.start(QUERY)
            self.assertEqual(second["task_id"], TASK["id"])
            self.client.start(QUERY)
            self.assertEqual([c.args[0] for c in request.call_args_list], ["POST", "POST", "GET"])
        self.assertEqual(sorted(p.name for p in (self.root / "requests").iterdir()), ["test-query-001.json"])

    def test_rejected_request_id_still_refuses_changed_query(self):
        with patch.object(self.client, "request", side_effect=dayuan.Error("AUTH_REQUIRED", definitive=True)):
            self.client.start(QUERY)
        with self.assertRaisesRegex(dayuan.Error, "REQUEST_CONFLICT"):
            self.client.start({**QUERY, "chat": "另一个群"})

    def test_unexpected_upstream_shapes_do_not_crash(self):
        for data in [{"task": {"id": "t", "status": "TASK_STATE_WORKING"}}, {"task": {"id": "t", "status": 2}}, {"task": "bad"},
                     {"id": ["t"], "contextId": 1, "status": None, "artifacts": "x", "history": [None, "x", {"role": "agent", "parts": None}]},
                     {"status": {"state": True, "message": "x"}, "message": None}, {"artifacts": [None, {"parts": "text"}]}]:
            result = dayuan.normalize(data)
            self.assertIsInstance(result["state"], str)
            self.assertIsInstance(result["text"], str)
            self.assertTrue(result["task_id"] is None or isinstance(result["task_id"], str))
        self.assertEqual(dayuan.normalize({"task": {"id": "t", "status": "TASK_STATE_WORKING"}})["state"], "working")
        self.assertEqual(dayuan.normalize({"status": {"state": 3}, "message": {"parts": [{"text": "ok"}]}})["text"], "ok")

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

    def fake_clock(self):
        clock = {"now": 0.0}
        self.client.clock = lambda: clock["now"]
        self.client.sleep = lambda seconds: clock.__setitem__("now", clock["now"] + seconds)
        return clock

    def test_read_waits_on_same_request_id_until_terminal(self):
        self.fake_clock()
        working = {**TASK, "status": {"state": "TASK_STATE_WORKING"}, "history": []}
        with patch.object(self.client, "request", side_effect=[working, working, TASK]) as request:
            result = self.client.read(QUERY, 30)
            self.assertEqual(result["state"], "completed")
            self.assertEqual(result["text"], "测试摘要")
            self.assertEqual([c.args[0] for c in request.call_args_list], ["POST", "GET", "GET"])
            self.assertTrue(all(c.args[1] == "/tasks/task_test_1?historyLength=0" for c in request.call_args_list[1:]))

    def test_wait_is_bounded_and_reports_pending_state(self):
        clock = self.fake_clock()
        working = {**TASK, "status": {"state": "TASK_STATE_WORKING"}, "history": []}
        with patch.object(self.client, "request", return_value=working) as request:
            result = self.client.read(QUERY, 10)
        self.assertEqual(result["state"], "working")
        self.assertIn("next_action", result)
        self.assertLessEqual(clock["now"], 10)
        self.assertEqual(request.call_args_list[0].args[0], "POST")
        self.assertEqual({c.args[0] for c in request.call_args_list[1:]}, {"GET"})
        with patch.object(self.client, "request", return_value=working) as request:
            self.assertEqual(self.client.read(QUERY, 0)["state"], "working")
            self.assertEqual(request.call_count, 1)

    def test_poll_failure_keeps_task_instead_of_resubmitting(self):
        self.fake_clock()
        working = {**TASK, "status": {"state": "TASK_STATE_WORKING"}, "history": []}
        with patch.object(self.client, "request", side_effect=[working, dayuan.Error("TRANSPORT_UNCERTAIN")]) as request:
            result = self.client.read(QUERY, 30)
        self.assertEqual(result["state"], "working")
        self.assertEqual(result["task_id"], TASK["id"])
        self.assertEqual(result["poll_error"], "TRANSPORT_UNCERTAIN")
        self.assertEqual(request.call_count, 2)

    def test_rejected_submit_is_returned_without_waiting(self):
        clock = self.fake_clock()
        with patch.object(self.client, "request", side_effect=dayuan.Error("AUTH_REQUIRED", definitive=True)):
            self.assertEqual(self.client.read(QUERY, 30)["state"], "rejected")
        self.assertEqual(clock["now"], 0)

    def test_wait_seconds_is_validated(self):
        for value in [-1, 31, True, 1.5, "5", None]:
            with self.assertRaisesRegex(dayuan.Error, "INVALID_WAIT_SECONDS"):
                dayuan.wait_seconds(value, dayuan.MCP_MAX_WAIT)
        self.assertEqual(dayuan.wait_seconds(30, dayuan.MCP_MAX_WAIT), 30)

    def test_cli_wait_limit_is_checked_before_submit(self):
        proc = subprocess.run([sys.executable, str(Path(dayuan.__file__)), "--state-dir", str(self.root), "read", "--wait", "301", "--json", json.dumps(QUERY)], text=True, capture_output=True, env={**os.environ, "DAYUAN_ACCESS_TOKEN": "test-private-token"}, timeout=5)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["error"], "INVALID_WAIT_SECONDS")
        self.assertFalse((self.root / "requests").exists())

    def rpc(self, *messages):
        inp = "\n".join(json.dumps(v) for v in messages) + "\n"
        proc = subprocess.run([sys.executable, str(Path(dayuan.__file__)), "--state-dir", str(self.root), "serve"], input=inp, text=True, capture_output=True, env={**os.environ, "DAYUAN_ACCESS_TOKEN": "test-private-token"}, timeout=5)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return [json.loads(x) for x in proc.stdout.splitlines()]

    def test_mcp_advertises_flow_wait_and_read_only_hints(self):
        init, tools = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertIn("request_id", init["result"]["instructions"])
        self.assertIn("dayuan_read_result", init["result"]["instructions"])
        by_name = {t["name"]: t for t in tools["result"]["tools"]}
        self.assertTrue(by_name["dayuan_status"]["annotations"]["readOnlyHint"])
        self.assertTrue(by_name["dayuan_read_result"]["annotations"]["readOnlyHint"])
        self.assertFalse(by_name["dayuan_read_start"]["annotations"]["readOnlyHint"])
        for name in ("dayuan_read_start", "dayuan_read_result"):
            self.assertEqual(by_name[name]["inputSchema"]["properties"]["wait_seconds"]["maximum"], 30)
        self.assertNotIn("wait_seconds", dayuan.QUERY_SCHEMA["properties"])

    def test_mcp_rejects_wait_above_limit_before_network(self):
        (reply,) = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "dayuan_read_start", "arguments": {**QUERY, "wait_seconds": 31}}})
        self.assertTrue(reply["result"]["isError"])
        self.assertEqual(reply["result"]["content"][0]["text"], "INVALID_WAIT_SECONDS")
        self.assertFalse((self.root / "requests").exists())

    def test_mcp_wait_passes_through_to_client(self):
        with patch.object(dayuan.Client, "read", return_value={"request_id": "x", "state": "completed"}) as read, \
             patch.object(dayuan.Client, "read_result", return_value={"request_id": "x", "state": "working"}) as read_result, \
             patch("sys.stdin", io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "dayuan_read_start", "arguments": {**QUERY, "wait_seconds": 20}}}) + "\n" +
                                           json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "dayuan_read_result", "arguments": {"request_id": QUERY["request_id"]}}}) + "\n")), \
             patch("sys.stdout", new_callable=io.StringIO) as out:
            dayuan.serve(self.root)
        self.assertEqual(read.call_args.args, (QUERY, 20))
        self.assertEqual(read_result.call_args.args, (QUERY["request_id"], 0))
        self.assertEqual(len(out.getvalue().splitlines()), 2)

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
