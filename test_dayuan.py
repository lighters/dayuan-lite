import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
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

    def hold_request_lock(self, rid=QUERY["request_id"]):
        fd = os.open(str(self.root / ".request-locks" / (rid + ".lock")), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, fd)
        return fd

    def test_busy_request_id_waits_then_returns_the_holders_task(self):
        with patch.object(self.client, "request", return_value=TASK):
            self.client.start(QUERY)
        self.client.REQUEST_LOCK_WAIT = 0.3
        dayuan.private_dir(self.root / ".request-locks")
        self.hold_request_lock()
        with patch.object(self.client, "request", return_value=TASK) as request:
            value = self.client.start(QUERY)
        self.assertEqual(value["task_id"], TASK["id"])
        self.assertEqual([c.args[0] for c in request.call_args_list], ["GET"])
        with self.assertRaisesRegex(dayuan.Error, "REQUEST_CONFLICT"):
            self.client.start({**QUERY, "chat": "另一个群"})

    def test_busy_request_id_without_task_is_reported_busy(self):
        self.client.REQUEST_LOCK_WAIT = 0.3
        dayuan.private_dir(self.root / ".request-locks")
        self.hold_request_lock()
        with patch.object(self.client, "request", return_value=TASK) as request:
            with self.assertRaisesRegex(dayuan.Error, "REQUEST_BUSY"):
                self.client.start(QUERY)
            dayuan.save_private(self.client.record_path(QUERY["request_id"]), {"request_id": QUERY["request_id"], "input_hash": dayuan.query_fingerprint(dayuan.validate_query(QUERY)), "credential_fingerprint": dayuan.digest("test-private-token"), "state": "dispatching"})
            with self.assertRaisesRegex(dayuan.Error, "REQUEST_BUSY"):
                self.client.start(QUERY)
        request.assert_not_called()

    def test_lock_released_during_wait_lets_the_submit_proceed(self):
        dayuan.private_dir(self.root / ".request-locks")
        fd = self.hold_request_lock()
        timer = threading.Timer(0.3, fcntl.flock, (fd, fcntl.LOCK_UN))
        timer.start()
        self.addCleanup(timer.cancel)
        with patch.object(self.client, "request", return_value=TASK) as request:
            value = self.client.start(QUERY)
        self.assertEqual(value["task_id"], TASK["id"])
        self.assertEqual([c.args[0] for c in request.call_args_list], ["POST"])

    def test_concurrent_rejected_retries_submit_only_once(self):
        self.client.REQUEST_LOCK_WAIT = 0.3
        with patch.object(self.client, "request", side_effect=dayuan.Error("AUTH_REQUIRED", definitive=True)):
            self.client.start(QUERY)
        loaded = threading.Event()
        contender_done = threading.Event()
        original_load = dayuan.load_private
        errors = []
        posted = []

        def load(path):
            value = original_load(path)
            if threading.current_thread().name == "paused-retry" and value.get("state") == "rejected":
                loaded.set()
                if not contender_done.wait(5):
                    raise AssertionError("contender did not finish")
            return value

        def request(method, path, body=None):
            if method == "POST":
                posted.append(body["message"]["messageId"])
            return TASK

        def paused_retry():
            try:
                self.client.start(QUERY)
            except (dayuan.Error, AssertionError) as exc:
                errors.append(str(exc))

        with patch.object(dayuan, "load_private", side_effect=load), patch.object(self.client, "request", side_effect=request):
            thread = threading.Thread(target=paused_retry, name="paused-retry")
            thread.start()
            try:
                self.assertTrue(loaded.wait(5))
                try:
                    self.client.start(QUERY)
                except dayuan.Error as exc:
                    self.assertEqual(str(exc), "REQUEST_BUSY")
            finally:
                contender_done.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(posted), 1)
        self.assertEqual(dayuan.load_private(self.client.record_path(QUERY["request_id"]))["task_id"], TASK["id"])

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

    def test_terminal_result_is_cached_in_the_receipt_and_not_refetched(self):
        working = {**TASK, "status": {"state": "TASK_STATE_WORKING"}, "history": []}
        for final_state in ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED"):
            rid = QUERY["request_id"] + final_state[-3:].lower()
            query = {**QUERY, "request_id": rid}
            final = {**TASK, "status": {"state": final_state}}
            with patch.object(self.client, "request", side_effect=[working, final]) as request:
                self.client.start(query)
                first = self.client.result(rid)
            self.assertEqual([c.args[0] for c in request.call_args_list], ["POST", "GET"])
            self.assertEqual(first["text"], "测试摘要")
            with patch("urllib.request.OpenerDirector.open") as network, patch.object(self.client, "request") as request:
                second = self.client.result(rid)
                waited = self.client.read_result(rid, 10)
            network.assert_not_called()
            request.assert_not_called()
            self.assertEqual(second, first)
            self.assertEqual(waited, first)
            self.assertEqual(second["state"], final_state.removeprefix("TASK_STATE_").lower())
            raw = (self.root / "requests" / (rid + ".json")).read_text()
            self.assertIn("测试摘要", raw)
            for secret in ("test-private-token", "private query", "ROLE_AGENT", "history", "TASK_STATE_", "artifacts"):
                self.assertNotIn(secret, raw)
            record = dayuan.load_private(self.client.record_path(rid))
            self.assertEqual(record["result"]["task_id"], TASK["id"])
            self.assertFalse(record["result"]["raw_messages_verified"])
            self.assertIn("limitation", record["result"])

    def test_pending_result_is_fetched_every_time_and_not_cached(self):
        working = {**TASK, "status": {"state": "TASK_STATE_WORKING"}, "history": []}
        with patch.object(self.client, "request", return_value=working) as request:
            self.client.start(QUERY)
            self.assertEqual(self.client.result(QUERY["request_id"])["state"], "working")
            self.assertEqual(self.client.result(QUERY["request_id"])["state"], "working")
        self.assertEqual([c.args[0] for c in request.call_args_list], ["POST", "GET", "GET"])
        self.assertNotIn("result", dayuan.load_private(self.client.record_path(QUERY["request_id"])))

    def test_cached_result_is_not_served_to_another_credential(self):
        with patch.object(self.client, "request", return_value=TASK):
            self.client.start(QUERY)
            self.client.result(QUERY["request_id"])
        self.assertIn("result", dayuan.load_private(self.client.record_path(QUERY["request_id"])))
        other = dayuan.Client(self.root, token="different-account-token")
        with patch("urllib.request.OpenerDirector.open") as network:
            with self.assertRaisesRegex(dayuan.Error, "CREDENTIAL_CHANGED_OR_MISSING"):
                other.result(QUERY["request_id"])
        network.assert_not_called()

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

    def test_wait_does_not_poll_after_sleep_reaches_deadline(self):
        clock = self.fake_clock()
        value = {"request_id": QUERY["request_id"], "state": "working"}
        with patch.object(self.client, "result", return_value=value) as result:
            self.client.wait(value, 1)
        self.assertEqual(clock["now"], 1)
        result.assert_not_called()

    def test_poll_network_timeout_uses_remaining_wait_budget(self):
        clock = self.fake_clock()
        working = {**TASK, "status": {"state": "TASK_STATE_WORKING"}, "history": []}
        with patch.object(self.client, "request", return_value=working):
            self.client.start(QUERY)
        timeouts = []

        def network(req, timeout):
            timeouts.append(timeout)
            clock["now"] += timeout
            raise TimeoutError()

        with patch("urllib.request.OpenerDirector.open", side_effect=network):
            result = self.client.wait({"request_id": QUERY["request_id"], "state": "working"}, 10)
        self.assertEqual(timeouts, [7])
        self.assertEqual(clock["now"], 10)
        self.assertEqual(result["state"], "working")
        self.assertIsNone(self.client.poll_deadline)

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
        self.assertEqual(len(replies[1]["result"]["tools"]), 6)
        self.assertNotIn("test-private-token", proc.stdout + proc.stderr)



class ReceiptMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {k: v for k, v in os.environ.items() if k not in ("DAYUAN_STATE_DIR", "DAYUAN_ACCESS_TOKEN")}

    def receipt(self, rid, state, created_at, task_id="task_" + "x" * 8, lock=True):
        record = {"request_id": rid, "input_hash": dayuan.digest("query"), "credential_fingerprint": dayuan.digest("test-private-token"), "message_id": "m-" + rid, "created_at": created_at, "state": state}
        if task_id:
            record["task_id"] = task_id
        dayuan.save_private(self.root / "requests" / (rid + ".json"), record)
        if lock:
            dayuan.private_dir(self.root / ".request-locks")
            os.close(os.open(str(self.root / ".request-locks" / (rid + ".lock")), os.O_CREAT | os.O_RDWR, 0o600))

    def cli(self, *argv):
        proc = subprocess.run([sys.executable, str(Path(dayuan.__file__)), "--state-dir", str(self.root), *argv], text=True, capture_output=True, env=self.env, timeout=5)
        return proc, json.loads(proc.stdout)

    def test_list_orders_newest_first_filters_by_state_and_stays_offline(self):
        self.receipt("old-done-000001", "completed", "2026-01-01T00:00:00+00:00")
        self.receipt("new-work-000002", "working", "2026-03-01T00:00:00+00:00")
        self.receipt("mid-unknown-0003", "outcome_unknown", "2026-02-01T00:00:00+00:00", task_id=None)
        with patch("urllib.request.OpenerDirector.open") as network:
            receipts = dayuan.list_receipts(self.root)
            filtered = dayuan.list_receipts(self.root, "working")
        network.assert_not_called()
        self.assertEqual([r["request_id"] for r in receipts], ["new-work-000002", "mid-unknown-0003", "old-done-000001"])
        self.assertEqual(receipts[1], {"request_id": "mid-unknown-0003", "state": "outcome_unknown", "task_id": None, "created_at": "2026-02-01T00:00:00+00:00"})
        self.assertEqual([r["request_id"] for r in filtered], ["new-work-000002"])
        self.assertNotIn("input_hash", json.dumps(receipts))
        self.assertNotIn(dayuan.digest("test-private-token"), json.dumps(receipts))
        self.assertEqual(dayuan.list_receipts(self.root / "missing"), [])
        with self.assertRaisesRegex(dayuan.Error, "INVALID_STATE_FILTER"):
            dayuan.list_receipts(self.root, "Completed; rm")
        proc, value = self.cli("list", "--state", "completed")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([r["request_id"] for r in value["receipts"]], ["old-done-000001"])
        self.assertEqual(value["state_dir"], str(self.root))
        self.assertFalse((self.root / "credentials.json").exists())

    def test_list_skips_receipts_removed_during_the_scan(self):
        rid = "removed-during-scan"
        self.receipt(rid, "completed", "2020-01-01T00:00:00+00:00")
        load = dayuan.load_private
        def removed(path):
            path.unlink()
            return load(path)
        with patch("dayuan.load_private", side_effect=removed):
            self.assertEqual(dayuan.list_receipts(self.root), [])

    def test_gc_previews_then_deletes_only_old_terminal_receipts_and_keeps_locks(self):
        old, recent = "2020-01-01T00:00:00+00:00", dayuan.now()
        for rid, state in [("old-completed-01", "completed"), ("old-failed-0001", "failed"), ("old-cancelled-1", "cancelled"), ("old-rejected-01", "rejected")]:
            self.receipt(rid, state, old, task_id=None if state == "rejected" else "task_" + rid)
        self.receipt("old-nolock-0001", "completed", old, lock=False)
        self.receipt("new-completed-1", "completed", recent)
        for rid, state in [("old-unknown-001", "outcome_unknown"), ("old-dispatch-01", "dispatching"), ("old-submitted-1", "submitted"), ("old-working-001", "working")]:
            self.receipt(rid, state, old, task_id=None if state in ("outcome_unknown", "dispatching") else "task_" + rid)
        before = sorted(p.name for p in (self.root / "requests").iterdir())
        with patch("urllib.request.OpenerDirector.open") as network:
            preview = dayuan.gc_receipts(self.root, 30)
        network.assert_not_called()
        self.assertTrue(preview["dry_run"])
        self.assertEqual(sorted(r["request_id"] for r in preview["receipts"]), ["old-cancelled-1", "old-completed-01", "old-failed-0001", "old-nolock-0001", "old-rejected-01"])
        self.assertEqual(preview["skipped_busy"], [])
        self.assertEqual(sorted(p.name for p in (self.root / "requests").iterdir()), before)
        self.assertFalse((self.root / ".request-locks" / "old-nolock-0001.lock").exists())
        applied = dayuan.gc_receipts(self.root, 30, apply=True)
        self.assertFalse(applied["dry_run"])
        self.assertEqual(sorted(r["request_id"] for r in applied["receipts"]), sorted(r["request_id"] for r in preview["receipts"]))
        self.assertEqual(sorted(p.stem for p in (self.root / "requests").iterdir()), ["new-completed-1", "old-dispatch-01", "old-submitted-1", "old-unknown-001", "old-working-001"])
        self.assertEqual(sorted(p.stem for p in (self.root / ".request-locks").iterdir()), sorted(p.removesuffix(".json") for p in before))
        self.assertEqual(dayuan.gc_receipts(self.root, 30, apply=True)["receipts"], [])

    def test_gc_rechecks_retried_receipts_after_taking_the_lock(self):
        client = dayuan.Client(self.root, token="test-private-token")
        listing = dayuan.list_receipts
        for uncertain in (False, True):
            with self.subTest(uncertain=uncertain):
                rid = QUERY["request_id"] + ("-unknown" if uncertain else "-submitted")
                query = {**QUERY, "request_id": rid}
                with patch.object(client, "request", side_effect=dayuan.Error("AUTH_REQUIRED", definitive=True)):
                    client.start(query)
                path = client.record_path(rid)
                record = dayuan.load_private(path)
                record["created_at"] = "2020-01-01T00:00:00+00:00"
                dayuan.save_private(path, record)
                def snapshot_then_retry(state):
                    snapshot = listing(state)
                    response = dayuan.Error("TRANSPORT_ERROR") if uncertain else {**TASK, "status": {"state": "TASK_STATE_SUBMITTED"}}
                    with patch.object(client, "request", side_effect=[response]) as request:
                        client.start(query)
                    self.assertEqual([c.args[0] for c in request.call_args_list], ["POST"])
                    return snapshot
                with patch("dayuan.list_receipts", side_effect=snapshot_then_retry):
                    value = dayuan.gc_receipts(self.root, 30, apply=True)
                self.assertEqual(value["receipts"], [])
                self.assertEqual(dayuan.load_private(path)["state"], "outcome_unknown" if uncertain else "submitted")

    def test_gc_rechecks_age_after_taking_the_lock(self):
        rid = "recent-replaced-01"
        self.receipt(rid, "completed", "2020-01-01T00:00:00+00:00")
        listing = dayuan.list_receipts
        def snapshot_then_replace(state):
            snapshot = listing(state)
            self.receipt(rid, "completed", dayuan.now())
            return snapshot
        with patch("dayuan.list_receipts", side_effect=snapshot_then_replace):
            self.assertEqual(dayuan.gc_receipts(self.root, 30, apply=True)["receipts"], [])
        self.assertTrue((self.root / "requests" / (rid + ".json")).exists())

    def test_gc_keeps_the_lock_inode_used_by_waiting_starters(self):
        rid = "waiting-starter-01"
        self.receipt(rid, "completed", "2020-01-01T00:00:00+00:00")
        lock = self.root / ".request-locks" / (rid + ".lock")
        waiting = os.open(str(lock), os.O_RDWR)
        self.addCleanup(os.close, waiting)
        inode = os.fstat(waiting).st_ino
        dayuan.gc_receipts(self.root, 30, apply=True)
        self.assertTrue(lock.exists())
        self.assertEqual(lock.stat().st_ino, inode)
        fcntl.flock(waiting, fcntl.LOCK_EX | fcntl.LOCK_NB)
        other = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, other)
        with self.assertRaises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_gc_creates_a_missing_lock_before_rechecking_and_deleting(self):
        rid = "missing-lock-0001"
        self.receipt(rid, "completed", "2020-01-01T00:00:00+00:00", lock=False)
        load = dayuan.load_private
        calls = []
        def inspect_lock(path):
            if calls:
                lock = self.root / ".request-locks" / (rid + ".lock")
                fd = os.open(str(lock), os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(fd)
            calls.append(path)
            return load(path)
        with patch("dayuan.load_private", side_effect=inspect_lock):
            value = dayuan.gc_receipts(self.root, 30, apply=True)
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual([entry["request_id"] for entry in value["receipts"]], [rid])
        self.assertTrue((self.root / ".request-locks" / (rid + ".lock")).exists())

    def test_gc_skips_a_receipt_whose_lock_is_held(self):
        old = "2020-01-01T00:00:00+00:00"
        self.receipt("held-completed-1", "completed", old)
        self.receipt("free-completed-1", "completed", old)
        fd = os.open(str(self.root / ".request-locks" / "held-completed-1.lock"), os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        value = dayuan.gc_receipts(self.root, 7, apply=True)
        self.assertEqual([r["request_id"] for r in value["receipts"]], ["free-completed-1"])
        self.assertEqual([r["request_id"] for r in value["skipped_busy"]], ["held-completed-1"])
        self.assertTrue((self.root / "requests" / "held-completed-1.json").exists())
        self.assertTrue((self.root / ".request-locks" / "held-completed-1.lock").exists())
        self.assertFalse((self.root / "requests" / "free-completed-1.json").exists())
        self.assertTrue((self.root / ".request-locks" / "free-completed-1.lock").exists())

    def test_gc_cli_requires_days_suffix_and_yes_to_delete(self):
        self.receipt("old-completed-01", "completed", "2020-01-01T00:00:00+00:00")
        for bad in ("7", "7days", "d", "100000d"):
            proc, value = self.cli("gc", "--older-than", bad)
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(value["error"], "INVALID_OLDER_THAN")
        proc, value = self.cli("gc", "--older-than", "7d")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(value["dry_run"])
        self.assertEqual([r["request_id"] for r in value["receipts"]], ["old-completed-01"])
        self.assertIn("next_action", value)
        self.assertTrue((self.root / "requests" / "old-completed-01.json").exists())
        proc, value = self.cli("gc", "--older-than", "7d", "--yes")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(value["dry_run"])
        self.assertFalse((self.root / "requests" / "old-completed-01.json").exists())
        self.assertTrue((self.root / ".request-locks" / "old-completed-01.lock").exists())

    def test_maintenance_commands_are_not_mcp_tools(self):
        self.assertEqual([t["name"] for t in dayuan.TOOLS], ["dayuan_capabilities", "dayuan_status", "dayuan_read_start", "dayuan_read_result", "dayuan_read_reply"])


class EntryPointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {k: v for k, v in os.environ.items() if k not in ("DAYUAN_STATE_DIR", "DAYUAN_ACCESS_TOKEN", "XDG_CONFIG_HOME")}
        self.env["HOME"] = str(self.root / "home")

    def cli(self, *argv, env=None, script="dayuan.py"):
        proc = subprocess.run([sys.executable, str(Path(dayuan.__file__).with_name(script)), *argv], text=True, capture_output=True, env=env or self.env, timeout=5)
        # status/read print one indented document; auth commands print one JSON line per step.
        out = proc.stdout.strip()
        return proc, json.loads(out if out.startswith("{\n") else out.splitlines()[-1])

    def test_default_state_dir_is_per_user_config(self):
        proc, value = self.cli("status")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(value["state_dir"], str(self.root / "home/.config/dayuan-lite"))
        proc, value = self.cli("status", env={**self.env, "XDG_CONFIG_HOME": str(self.root / "xdg")})
        self.assertEqual(value["state_dir"], str(self.root / "xdg/dayuan-lite"))
        proc, value = self.cli("status", env={**self.env, "XDG_CONFIG_HOME": "relative/ignored"})
        self.assertEqual(value["state_dir"], str(self.root / "home/.config/dayuan-lite"))
        self.assertFalse((self.root / "home/.config/dayuan-lite").exists())

    def test_state_dir_overrides_before_or_after_command(self):
        env_dir, arg_dir = self.root / "env-state", self.root / "arg-state"
        self.assertEqual(self.cli("status", env={**self.env, "DAYUAN_STATE_DIR": str(env_dir)})[1]["state_dir"], str(env_dir))
        for argv in (["--state-dir", str(arg_dir), "status"], ["status", "--state-dir", str(arg_dir)]):
            self.assertEqual(self.cli(*argv, env={**self.env, "DAYUAN_STATE_DIR": str(env_dir)})[1]["state_dir"], str(arg_dir))

    def test_unified_entry_dispatches_auth_commands(self):
        with patch("workbuddy_auth.run", return_value=0) as run:
            for argv, call in [(["login", "--open", "--wait", "300"], ("login", True, 300)), (["--state-dir", str(self.root), "finish"], ("finish", False, 0)),
                               (["renew", "--state-dir", str(self.root)], ("renew", False, 0)), (["wecom-login"], ("wecom-login", False, 0))]:
                with patch.dict(os.environ, {"DAYUAN_STATE_DIR": str(self.root)}), self.assertRaises(SystemExit) as stop:
                    dayuan.main(argv)
                self.assertEqual(stop.exception.code, 0)
                self.assertEqual(run.call_args.args, (call[0], self.root, call[1], call[2]))
        proc, value = self.cli("wecom-login", "--state-dir", str(self.root))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(value["authorization_url"].startswith("https://www.workbuddy.cn/"))

    def test_legacy_auth_script_still_works(self):
        for script_args in (["status", "--state-dir", str(self.root)], ["--state-dir", str(self.root), "status"]):
            proc, value = self.cli(*script_args, script="workbuddy_auth.py")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(value["phase"], "logged_out")
        self.assertEqual(self.cli("status", "--state-dir", str(self.root))[1]["configured"], False)

    def test_legacy_state_is_detected_but_never_moved(self):
        legacy, default = self.root / "repo/.private", self.root / "home/.config/dayuan-lite"
        dayuan.private_dir(legacy)
        dayuan.save_private(legacy / "cloud-auth.json", {"phase": "ready"})
        with patch.object(dayuan, "LEGACY_STATE", legacy), patch.object(dayuan, "DEFAULT_STATE", default), patch.dict(os.environ, self.env, clear=True):
            self.assertIsNotNone(dayuan.migration_hint(default, False))
            self.assertIsNone(dayuan.migration_hint(default, True))
            with patch("sys.stdout", new_callable=io.StringIO) as out, patch("sys.stderr", new_callable=io.StringIO) as err:
                dayuan.main(["status"])
            value = json.loads(out.getvalue())
            self.assertEqual(value["migration_hint"]["legacy_state_dir"], str(legacy))
            self.assertIn(str(legacy), err.getvalue())
            with patch("sys.stdout", new_callable=io.StringIO) as out, patch("sys.stderr", new_callable=io.StringIO) as err:
                dayuan.main(["--state-dir", str(legacy), "status"])
            self.assertNotIn("migration_hint", json.loads(out.getvalue()))
            self.assertEqual(err.getvalue(), "")
            dayuan.save_private(default / "credentials.json", {"access_token": "x"})
            self.assertIsNone(dayuan.migration_hint(default, False))
        self.assertTrue((legacy / "cloud-auth.json").exists())
        self.assertEqual(sorted(p.name for p in default.iterdir()), ["credentials.json"])

    def test_pyproject_declares_stdlib_only_console_script(self):
        try:
            import tomllib
        except ImportError:
            self.skipTest("tomllib requires Python 3.11+")
        project = tomllib.loads(Path(dayuan.__file__).with_name("pyproject.toml").read_text())
        self.assertEqual(project["project"]["scripts"], {"dayuan": "dayuan:main"})
        self.assertEqual(project["project"]["dependencies"], [])
        self.assertEqual(project["project"]["requires-python"], ">=3.9")
        self.assertEqual(sorted(project["tool"]["setuptools"]["py-modules"]), ["dayuan", "wecom_bridge", "workbuddy_auth"])
        self.assertNotIn("version", project["project"])
        self.assertEqual(project["tool"]["setuptools"]["dynamic"]["version"], {"attr": "dayuan.__version__"})


if __name__ == "__main__":
    unittest.main()


class UnifiedKitTests(unittest.TestCase):
    setUp = DayuanTests.setUp
    tearDown = DayuanTests.tearDown
    hold_request_lock = DayuanTests.hold_request_lock
    rpc = DayuanTests.rpc
    def waiting(self):
        return {**TASK, "status": {"state": "TASK_STATE_INPUT_REQUIRED", "message": {"parts": [{"text": "请选择候选群"}]}}}

    def reply_query(self, **changes):
        return {"request_id": "reply-query-001", "parent_request_id": QUERY["request_id"], "answer": "选择第一个候选群", **changes}

    def seed_waiting(self):
        with patch.object(self.client, "request", return_value=self.waiting()):
            self.client.start(QUERY)

    def test_interaction_uses_current_question_instead_of_old_artifacts(self):
        task = {**self.waiting(), "artifacts": [{"parts": [{"text": "上轮摘要"}]}]}
        value = dayuan.normalize(task)
        self.assertEqual(value["text"], "请选择候选群")
        self.assertEqual(value["interaction"], {"kind": "clarification", "reply_allowed": True, "protocol": "text"})
        self.assertTrue(value["requires_user_action"])
        self.assertFalse(value["raw_messages_verified"])

    def test_approval_detection_does_not_expose_card_payload(self):
        cards = [
            {"metadata": {"wecom_part_type": "approval"}, "data": {"token_id_list": ["fake-card-secret"]}},
            {"data": {"token_id_list": []}},
            {"data": {"token_id_list": "fake-card-secret"}},
            {"metadata": {"wecom_part_type": "approval"}, "text": "fake-card-secret"},
        ]
        for card in cards:
            with self.subTest(card=cards.index(card)):
                task = {**self.waiting(), "status": {"state": 6, "message": {"parts": [{"text": "fake-card-secret"}, card]}}}
                value = dayuan.normalize(task)
                self.assertEqual(value["interaction"], {"kind": "approval", "reply_allowed": False, "protocol": "out_of_band"})
                self.assertEqual(value["state"], "input_required")
                self.assertEqual(value["text"], "")
                self.assertIn("人工", value["next_action"])
                self.assertNotIn("fake-card-secret", json.dumps(value))
                self.assertNotIn("token_id_list", json.dumps(value))

    def test_unknown_or_missing_interaction_fails_closed(self):
        for parts in [[], None, [None], [{"data": {"question": "opaque"}}], [{"text": ""}], [{"text": "question"}, {"url": "https://example.invalid/card"}], [{"text": "question", "metadata": {"wecom_part_type": "future_card"}}], [{"text": "question", "metadata": {"future_card": True}}]]:
            with self.subTest(parts=parts):
                task = {**self.waiting(), "status": {"state": "TASK_STATE_INPUT_REQUIRED", "message": {"parts": parts}}}
                value = dayuan.normalize(task)
                self.assertEqual(value["interaction"]["kind"], "unsupported")
                self.assertFalse(value["interaction"]["reply_allowed"])
                self.assertEqual(value["text"], "")
        value = dayuan.normalize({**self.waiting(), "status": {"state": "TASK_STATE_AUTH_REQUIRED", "message": {"parts": [{"text": "question"}]}}})
        self.assertEqual(value["interaction"]["kind"], "authorization")
        self.assertFalse(value["interaction"]["reply_allowed"])

    def test_card_reply_only_reads_and_does_not_create_receipt_or_barrier(self):
        self.seed_waiting()
        for part, kind, code in [({"data": {"token_id_list": ["fake-card-secret"]}}, "approval", "APPROVAL_REQUIRED"), ({"data": {"question": "unknown"}}, "unsupported", "UNSUPPORTED_INTERACTION")]:
            task = {**self.waiting(), "status": {"state": 6, "message": {"parts": [part]}}}
            with patch.object(self.client, "request", return_value=task) as call:
                value = self.client.reply(self.reply_query())
            self.assertEqual([c.args[0] for c in call.call_args_list], ["GET"])
            self.assertEqual(value["request_id"], QUERY["request_id"])
            self.assertEqual(value["reply_request_id"], "reply-query-001")
            self.assertFalse(value["reply_sent"])
            self.assertEqual(value["error"], code)
            self.assertEqual(value["interaction"]["kind"], kind)
            self.assertFalse(self.client.record_path("reply-query-001").exists())
            self.assertFalse((self.root / "reply-barriers").exists())
            for path in (self.root / "requests").glob("*.json"):
                self.assertNotIn("fake-card-secret", path.read_text())
        with patch.object(self.client, "request", side_effect=[self.waiting(), TASK]) as call:
            self.assertEqual(self.client.reply(self.reply_query())["state"], "completed")
        self.assertEqual([c.args[0] for c in call.call_args_list], ["GET", "POST"])

    def test_cli_and_mcp_route_cards_without_post_or_extra_polling(self):
        self.seed_waiting()
        task = {**self.waiting(), "status": {"state": 6, "message": {"parts": [{"metadata": {"wecom_part_type": "approval"}, "data": {"token_id_list": ["fake-card-secret"]}}]}}}
        with patch.object(dayuan.Client, "request", return_value={"task": task}) as call, patch.dict(os.environ, {"DAYUAN_ACCESS_TOKEN": "test-private-token"}):
            output = io.StringIO()
            with patch("sys.stdout", output):
                dayuan.main(["--state-dir", str(self.root), "reply", "--json", json.dumps(self.reply_query()), "--wait", "30"])
            value = json.loads(output.getvalue())
            self.assertFalse(value["reply_sent"])
            messages = [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "dayuan_read_result", "arguments": {"request_id": QUERY["request_id"], "wait_seconds": 30}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "dayuan_read_reply", "arguments": {**self.reply_query(), "wait_seconds": 30}}},
                {"jsonrpc": "2.0", "id": 3, "method": "ping"}]
            output = io.StringIO()
            with patch("sys.stdin", io.StringIO("\n".join(json.dumps(m) for m in messages))), patch("sys.stdout", output):
                dayuan.serve(self.root)
            replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([c.args[0] for c in call.call_args_list], ["GET", "GET", "GET"])
        for response in replies[:2]:
            value = json.loads(response["result"]["content"][0]["text"])
            self.assertEqual(value["request_id"], QUERY["request_id"])
            self.assertEqual(value["interaction"]["kind"], "approval")
            self.assertNotIn("fake-card-secret", json.dumps(response))
        self.assertEqual(replies[2]["result"], {})
        self.assertFalse(self.client.record_path("reply-query-001").exists())
        self.assertFalse((self.root / "reply-barriers").exists())

    def test_card_guard_preserves_rejected_receipt_and_unresolved_barrier(self):
        self.seed_waiting()
        with patch.object(self.client, "request", side_effect=[self.waiting(), dayuan.Error("UPSTREAM_HTTP_400", definitive=True)]):
            self.assertEqual(self.client.reply(self.reply_query())["state"], "rejected")
        receipt = self.client.record_path("reply-query-001")
        barrier = self.root / "reply-barriers" / (QUERY["request_id"] + ".json")
        before = receipt.read_bytes(), barrier.read_bytes()
        task = {**self.waiting(), "status": {"state": 6, "message": {"parts": [{"data": {"token_id_list": ["fake-card-secret"]}}]}}}
        with patch.object(self.client, "request", return_value=task) as call:
            self.assertFalse(self.client.reply(self.reply_query())["reply_sent"])
        self.assertEqual([c.args[0] for c in call.call_args_list], ["GET"])
        self.assertEqual((receipt.read_bytes(), barrier.read_bytes()), before)
        record = dayuan.load_private(receipt)
        record["state"] = "outcome_unknown"
        dayuan.save_private(receipt, record)
        dayuan.save_private(barrier, {"request_id": "reply-query-001", "unresolved": True})
        with patch.object(self.client, "request") as call:
            with self.assertRaisesRegex(dayuan.Error, "REPLY_OUTCOME_UNKNOWN"):
                self.client.reply(self.reply_query(request_id="reply-query-002"))
            self.assertEqual(self.client.reply(self.reply_query())["state"], "outcome_unknown")
        call.assert_not_called()

    def test_reply_continues_same_task_and_deduplicates(self):
        self.seed_waiting()
        with patch.object(self.client, "request", side_effect=[self.waiting(), TASK]) as call:
            value = self.client.reply(self.reply_query())
        self.assertEqual(value["task_id"], TASK["id"])
        body = call.call_args.args[2]
        self.assertEqual(body["message"]["taskId"], TASK["id"])
        self.assertEqual(body["message"]["contextId"], TASK["contextId"])
        self.assertIn("禁止发送", body["message"]["parts"][0]["text"])
        with patch.object(self.client, "request", return_value=TASK) as call:
            again = self.client.reply(self.reply_query())
        self.assertEqual(again["task_id"], TASK["id"])
        self.assertNotIn("POST", [c.args[0] for c in call.call_args_list])
        record = self.client.record_path("reply-query-001").read_text()
        self.assertNotIn("选择第一个", record)
        self.assertNotIn("test-private-token", record)

    def test_reply_rejects_invalid_terminal_and_changed_identity(self):
        self.seed_waiting()
        for changes in [{"answer": ""}, {"answer": "x" * 2001}, {"answer": True}, {"answer": "x\nwrite"}, {"extra": 1}, {"request_id": QUERY["request_id"]}]:
            with patch.object(self.client, "request") as call:
                with self.assertRaises(dayuan.Error):
                    self.client.reply(self.reply_query(**changes))
                call.assert_not_called()
        with patch.object(self.client, "request", return_value=TASK):
            with self.assertRaisesRegex(dayuan.Error, "TASK_NOT_INPUT_REQUIRED"):
                self.client.reply(self.reply_query())
        self.client.token = "another-account-token"
        with patch.object(self.client, "request") as call:
            with self.assertRaisesRegex(dayuan.Error, "CREDENTIAL_CHANGED"):
                self.client.reply(self.reply_query())
            call.assert_not_called()

    def test_uncertain_reply_blocks_replay_and_other_reply_ids(self):
        self.seed_waiting()
        with patch.object(self.client, "request", side_effect=[self.waiting(), dayuan.Error("TRANSPORT_UNCERTAIN")]):
            value = self.client.reply(self.reply_query())
        self.assertEqual(value["state"], "outcome_unknown")
        with patch.object(self.client, "request") as call:
            self.assertEqual(self.client.reply(self.reply_query())["state"], "outcome_unknown")
            self.assertEqual(self.client.result("reply-query-001")["state"], "outcome_unknown")
            with self.assertRaisesRegex(dayuan.Error, "REPLY_OUTCOME_UNKNOWN"):
                self.client.reply(self.reply_query(request_id="reply-query-002"))
            with self.assertRaisesRegex(dayuan.Error, "REQUEST_CONFLICT"):
                self.client.reply(self.reply_query(answer="另一个选择"))
            call.assert_not_called()

    def test_rejected_reply_can_retry_same_id_and_context_mismatch_is_uncertain(self):
        self.seed_waiting()
        with patch.object(self.client, "request", side_effect=[self.waiting(), dayuan.Error("AUTH_REQUIRED", definitive=True)]):
            self.assertTrue(self.client.reply(self.reply_query())["retry_safe"])
        with patch.object(self.client, "request", side_effect=[self.waiting(), {**TASK, "id": "wrong-task"}]):
            value = self.client.reply(self.reply_query())
        self.assertEqual(value["state"], "outcome_unknown")
        self.assertEqual(value["error"], "TASK_CONTEXT_MISMATCH")

    def test_reply_parent_lock_prevents_concurrent_send(self):
        self.seed_waiting()
        self.hold_request_lock()
        self.client.REQUEST_LOCK_WAIT = 0
        with patch.object(self.client, "request") as call:
            with self.assertRaisesRegex(dayuan.Error, "REQUEST_BUSY"):
                self.client.reply(self.reply_query())
            call.assert_not_called()

    def test_capabilities_local_never_refreshes_or_spawns(self):
        with patch.object(dayuan.Client, "prepare_auth") as auth, patch("wecom_bridge.run_cli") as spawn:
            value = dayuan.capabilities(self.root)
        auth.assert_not_called()
        spawn.assert_not_called()
        self.assertFalse(value["business_permissions_verified"])
        self.assertEqual(len(value["wecom_operations"]), 18)
        self.assertIn("dayuan", value["recovery"]["commands"][0])
        self.assertFalse(value["downloads_supported"])

    def test_setup_generates_portable_mcp_and_installs_skill_without_overwriting(self):
        target = self.root / "agent skills"
        value = dayuan.setup(self.root / "account space", target)
        entry = value["mcpServers"]["dayuan-lite"]
        self.assertEqual(entry["command"], sys.executable)
        self.assertEqual(entry["args"], ["-m", "dayuan", "--state-dir", str((self.root / "account space").resolve()), "serve"])
        skill = target / "dayuan-readonly" / "SKILL.md"
        self.assertTrue(skill.is_file())
        skill.write_text("user-owned")
        with self.assertRaisesRegex(dayuan.Error, "SKILL_TARGET_EXISTS"):
            dayuan.setup(self.root, target)
        self.assertEqual(skill.read_text(), "user-owned")

    def test_mcp_new_tools_are_callable_and_reject_unknown_arguments(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "dayuan_capabilities", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "dayuan_read_reply", "arguments": {**self.reply_query(), "wait_seconds": 31}}},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        ]
        replies = self.rpc(*messages)
        data = json.loads(replies[0]["result"]["content"][0]["text"])
        self.assertFalse(data["business_permissions_verified"])
        self.assertTrue(replies[1]["result"]["isError"])
        self.assertEqual(replies[2]["result"], {})

    def test_online_capabilities_checks_token_and_version_without_business_reads(self):
        with patch("workbuddy_auth.CloudAuth.ensure_wecom", return_value={"access_token": "test-private-token"}) as auth, patch("wecom_bridge.inspect_cli", return_value={"version": "1.3.4", "compatible": True}) as version, patch.object(dayuan.Client, "request") as request:
            value = dayuan.capabilities(self.root, online=True)
        auth.assert_called_once_with(force=True)
        version.assert_called_once()
        request.assert_not_called()
        self.assertTrue(value["authorization_verified"])
        self.assertFalse(value["business_permissions_verified"])
        self.assertNotIn("test-private-token", json.dumps(value))
        from workbuddy_auth import AuthError
        with patch("workbuddy_auth.CloudAuth.ensure_wecom", side_effect=AuthError("REFRESH_OUTCOME_UNKNOWN_LOGIN_REQUIRED")), patch("wecom_bridge.inspect_cli", side_effect=dayuan.Error("WECOM_CLI_NOT_FOUND")):
            value = dayuan.capabilities(self.root, online=True)
        self.assertEqual(value["auth_error"], "REFRESH_OUTCOME_UNKNOWN_LOGIN_REQUIRED")
        self.assertFalse(value["authorization_verified"])
        self.assertIn("login --open", value["recovery"]["commands"][0])
        self.assertIn("WECOM_CLI_NOT_FOUND", value["wecom_cli"]["error"])

    def test_recovery_uses_real_phases_and_quotes_state_paths(self):
        import shlex
        for phase in ("pending_token", "pending_account"):
            command = dayuan.recovery(self.root / "account space", phase=phase)["commands"][0]
            argv = shlex.split(command)
            self.assertEqual(argv[-3:], ["finish", "--wait", "60"])
            self.assertEqual(argv[2], str((self.root / "account space").resolve()))
        self.assertIn("wecom-login", dayuan.recovery(self.root, "WECOM_AUTH_REQUIRED_OPEN_WECOM_LOGIN")["commands"][0])

    def test_reply_second_round_uses_root_and_rejects_reply_as_parent(self):
        self.seed_waiting()
        with patch.object(self.client, "request", return_value=self.waiting()):
            self.client.reply(self.reply_query())
        with self.assertRaisesRegex(dayuan.Error, "REPLY_ROOT_REQUIRED"):
            self.client.reply(self.reply_query(request_id="reply-query-003", parent_request_id="reply-query-001"))
        with patch.object(self.client, "request", side_effect=[self.waiting(), TASK]) as call:
            value = self.client.reply(self.reply_query(request_id="reply-query-002", answer="时间范围按原查询"))
        self.assertEqual(value["state"], "completed")
        self.assertEqual(call.call_args.args[2]["message"]["taskId"], TASK["id"])

    def test_reply_crash_receipt_and_missing_barrier_receipt_fail_closed(self):
        self.seed_waiting()
        fingerprint = dayuan.query_fingerprint(self.reply_query())
        dayuan.save_private(self.client.record_path("reply-query-001"), {"request_id": "reply-query-001", "parent_request_id": QUERY["request_id"], "input_hash": fingerprint, "credential_fingerprint": dayuan.digest(self.client.token), "state": "dispatching"})
        dayuan.save_private(self.root / "reply-barriers" / (QUERY["request_id"] + ".json"), {"request_id": "reply-query-001", "unresolved": True})
        with patch.object(self.client, "request") as call:
            self.assertEqual(self.client.reply(self.reply_query())["state"], "outcome_unknown")
            with self.assertRaisesRegex(dayuan.Error, "REPLY_OUTCOME_UNKNOWN"):
                self.client.reply(self.reply_query(request_id="reply-query-002"))
            call.assert_not_called()
        self.client.record_path("reply-query-001").unlink()
        with self.assertRaisesRegex(dayuan.Error, "REPLY_OUTCOME_UNKNOWN"):
            self.client.reply(self.reply_query(request_id="reply-query-002"))

    def test_mcp_reply_dispatch_and_cli_reply_input_errors(self):
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "dayuan_read_reply", "arguments": {**self.reply_query(), "wait_seconds": 20}}}
        with patch.object(dayuan.Client, "reply", return_value={"state": "working"}) as reply, patch("sys.stdin", io.StringIO(json.dumps(message) + "\n")), patch("sys.stdout", new_callable=io.StringIO) as out:
            dayuan.serve(self.root)
        reply.assert_called_once_with(self.reply_query(), 20)
        self.assertEqual(json.loads(out.getvalue())["result"]["content"][0]["type"], "text")
        proc = subprocess.run([sys.executable, str(Path(dayuan.__file__)), "--state-dir", str(self.root), "reply", "--json", json.dumps(self.reply_query(answer=""))], text=True, capture_output=True, timeout=5)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["error"], "INVALID_REPLY_ARGUMENT")
        self.assertEqual(proc.stderr, "")

    def test_installer_preserves_spaces_and_passes_optional_flags(self):
        log = self.root / "install-log.jsonl"
        bins = self.root / "test bins"
        bins.mkdir()
        program = ("#!" + sys.executable + "\nimport json, os, sys\nfrom pathlib import Path\n"
                   "with open(os.environ['KIT_TEST_LOG'], 'a') as f: f.write(json.dumps([Path(sys.argv[0]).name, *sys.argv[1:]])+'\\n')\n"
                   "if sys.argv[1:]==['tool','dir','--bin']: print(os.environ['KIT_TEST_BIN'])\n")
        for name in ("uv", "dayuan", "npm"):
            path = bins / name
            path.write_text(program)
            path.chmod(0o700)
        env = {**os.environ, "PATH": str(bins) + ":/usr/bin:/bin", "KIT_TEST_LOG": str(log), "KIT_TEST_BIN": str(bins)}
        script = Path(dayuan.__file__).parent / "scripts" / "install.sh"
        proc = subprocess.run(["/bin/sh", str(script), "--with-wecom-cli", "--skill-dir", str(self.root / "my skills")], env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertIn(["uv", "tool", "install", "--upgrade", "--reinstall", str(Path(dayuan.__file__).parent)], calls)
        self.assertIn(["npm", "install", "--global", "@wecom/cli@1.3.4"], calls)
        self.assertIn(["dayuan", "setup", "--skill-dir", str(self.root / "my skills")], calls)
        self.assertEqual(calls[-1], ["dayuan", "doctor"])

    def test_cloud_server_error_does_not_restart_a_ready_login(self):
        from workbuddy_auth import AuthError
        with patch('workbuddy_auth.CloudAuth.status', return_value={'phase':'ready'}), patch('workbuddy_auth.CloudAuth.ensure_wecom', side_effect=AuthError('HTTP_500_CODE_10073')), patch('wecom_bridge.inspect_cli', return_value={'version':'1.3.4','compatible':True}):
            value = dayuan.capabilities(self.root, online=True)
        self.assertIn('doctor --online', value['recovery']['commands'][0])
        self.assertNotIn('login --open', value['recovery']['commands'][0])
        self.assertIn('login --open', dayuan.recovery(self.root,'HTTP_500_CODE_10073',phase='refresh_unknown')['commands'][0])

    def test_accepted_rejected_task_keeps_terminal_result_and_is_not_retryable(self):
        self.seed_waiting()
        rejected = {**TASK, 'status':{'state':'TASK_STATE_REJECTED'}}
        with patch.object(self.client, 'request', side_effect=[self.waiting(), rejected]):
            first = self.client.reply(self.reply_query())
        with patch.object(self.client, 'request') as call:
            again = self.client.result('reply-query-001')
            repeated = self.client.reply(self.reply_query())
            call.assert_not_called()
        for value in (again, repeated):
            self.assertEqual(value['task_id'], first['task_id'])
            self.assertEqual(value['text'], first['text'])
            self.assertEqual(value['evidence'], first['evidence'])
            self.assertFalse(value.get('retry_safe', False))
