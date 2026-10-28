#!/usr/bin/env python3
"""Adversarial handler regressions. Expectations: user approval invariant.

No JavaScript is executed. Fake child calls prove which payloads the real
gateway would forward; obfuscated snippets use JS property access semantics.
"""
import concurrent.futures
import json
import io
import threading
import unittest
from email.message import Message
from unittest.mock import patch
from gate_test_support import load_gateway, Transport

CODE = "await fs.rm('/tmp/example')"


class RedTeam(unittest.TestCase):
    def setUp(self):
        self.g = load_gateway()
        self.t = Transport(self.g)
        self.sid = self.t.initialize()

    def request(self, code=CODE, **args):
        return self.t.call(self.sid, dict(title="test", code=code, **args))[2]

    def pending(self):
        result = self.request()
        aid = result["error"]["data"]["approval_id"]
        return aid, self.g.APPROVALS[aid]

    def approve(self, aid, code, tok="APP"):
        return self.t.http("/approve", {"approval_id": aid, "code": code}, tok)[0]

    def test_requester_cannot_supply_even_correct_inline_code(self):
        aid, rec = self.pending()
        result = self.request(_approval_id=aid, _approval=rec["code"])
        self.assertIn("error", result)
        self.assertEqual(rec["status"], "pending")

    def test_guesses_never_compared_for_requester(self):
        aid, rec = self.pending()
        with patch.object(self.g.secrets, "compare_digest", side_effect=AssertionError("oracle")):
            self.assertEqual(self.approve(aid, rec["code"], "REQ"), 403)
            self.assertIn("error", self.request(_approval_id=aid, _approval=rec["code"]))

    def test_approver_wrong_attempts_lock_record(self):
        aid, rec = self.pending()
        wrong = "000000" if rec["code"] != "000000" else "111111"
        # UNVERIFIED policy choice: five failed approver attempts; not a measured safe rate.
        for _ in range(5):
            self.assertEqual(self.approve(aid, wrong), 403)
        self.assertEqual(self.approve(aid, rec["code"]), 403)

    def test_approver_uses_constant_time_comparison(self):
        aid, rec = self.pending()
        with patch.object(self.g.secrets, "compare_digest", wraps=self.g.secrets.compare_digest) as compare:
            self.assertEqual(self.approve(aid, rec["code"]), 200)
        self.assertTrue(compare.called)

    def test_obfuscated_code_requires_human(self):
        snippets = [
            "await fs['rm']('/tmp/example')",
            "await fs /* gap */ . rm('/tmp/example')",
            "const {rm: erase} = fs; await erase('/tmp/example')",
            "await fs.readFile('/tmp/.'+'env', 'utf8')",
            r"await fs.readFile('/tmp/\x2eenv', 'utf8')",
            "await fetch(url, {method: verb})",
            "console.log('read'); await action()",
            "await page.getByRole('button', {name:'Confirm'}).click()",
        ]
        for code in snippets:
            with self.subTest(code=code):
                self.assertIn("error", self.request(code))

    def test_off_or_unknown_mode_cannot_disable_invariant(self):
        for mode in ("off", "typo"):
            self.g.clients["REQ"]["gate"] = mode
            self.assertIn("error", self.request())

    def test_unknown_tool_alias_not_forwarded(self):
        child = self.g.SESSIONS[self.sid]["child"]
        for name in ("REPL", "execute", "repl "):
            with self.subTest(name=name):
                before = len(child.calls)
                response = self.t.call(self.sid, {"code": CODE}, tool=name)[2]
                self.assertIn("error", response)
                self.assertEqual(len(child.calls), before)

    def test_unknown_rpc_not_forwarded(self):
        child = self.g.SESSIONS[self.sid]["child"]
        before = len(child.calls)
        response = self.t.http("/mcp", {"id": 1, "method": "custom/execute", "params": {"code": CODE}}, "REQ", self.sid)[2]
        self.assertIn("error", response)
        self.assertEqual(len(child.calls), before)

    def test_jobs_cannot_bypass_obfuscated_code_gate(self):
        with patch.object(self.g.threading.Thread, "start") as start:
            response = self.t.call(self.sid, {"code": "await fs['rm']('/tmp/example')"}, tool="aside_job_start")[2]
        self.assertTrue(response["result"]["isError"])
        start.assert_not_called()

    def test_other_token_same_display_name_cannot_consume(self):
        aid, rec = self.pending()
        self.assertEqual(self.approve(aid, rec["code"]), 200)
        self.g.clients["OTHER"]["name"] = self.g.clients["REQ"]["name"]
        other_sid = self.t.initialize("OTHER")
        response = self.t.call(other_sid, {"title": "test", "code": CODE, "_approval_id": aid}, tok="OTHER")[2]
        self.assertIn("error", response)
        self.assertEqual(rec["status"], "approved")

    def test_other_client_session_not_forwarded(self):
        response = self.t.call(self.sid, {"code": "console.log('hello')"}, tok="OTHER")[2]
        self.assertIn("error", response)

    def test_approval_cannot_move_to_other_repl_context(self):
        aid, rec = self.pending()
        self.approve(aid, rec["code"])
        other_sid = self.t.initialize()
        response = self.t.call(other_sid, {"code": CODE, "_approval_id": aid})[2]
        self.assertIn("error", response)
        self.assertEqual(rec["status"], "approved")

    def test_named_session_cleanup_uses_token_identity(self):
        _, headers, _ = self.t.http("/mcp", {"id": 1, "method": "initialize"}, "REQ", headers={"X-Aside-Session": "named"})
        sid = headers["Mcp-Session-Id"]
        self.t.http("/mcp", {}, "REQ", sid, method="DELETE")
        self.assertFalse(self.g.NAMED)

    def test_mjpeg_does_not_execute_in_other_clients_session(self):
        self.g.MJPEG.update(session=None, client="foreign principal")
        self.assertIsNone(self.g._mjpeg_resolve_child())

    def test_named_mjpeg_resolves_own_token(self):
        self.t.http("/mcp", {"id": 1, "method": "initialize"}, "REQ", headers={"X-Aside-Session": "named"})
        with patch.object(self.g, "mjpeg_viewer_open", side_effect=RuntimeError("resolved")):
            with self.assertRaisesRegex(RuntimeError, "resolved"):
                self.t.http("/preview.mjpeg?session=named", {}, "REQ", method="GET")

    def test_other_client_cannot_delete_session(self):
        self.t.http("/mcp", {}, "OTHER", self.sid, method="DELETE")
        self.assertIn(self.sid, self.g.SESSIONS)

    def test_pending_not_approvable_until_delivery_finishes(self):
        decisions = []
        def send(_):
            aid, rec = next(iter(self.g.APPROVALS.items()))
            decisions.append(self.approve(aid, rec["code"]))
            return False
        self.g.tg_send = send
        self.request()
        self.assertEqual(decisions, [403])

    def test_concurrent_retry_executes_once(self):
        aid, rec = self.pending()
        self.approve(aid, rec["code"])
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.request(_approval_id=aid), range(8)))
        self.assertEqual(sum("error" not in r for r in results), 1)

    def test_concurrent_creation_deduplicates_and_sends_once(self):
        entered, release = threading.Event(), threading.Event()
        sent = []
        def send(text):
            sent.append(text)
            entered.set()
            self.assertTrue(release.wait(2))  # fixture deadlock watchdog, no latency claim
            return True
        self.g.tg_send = send
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.request)
            self.assertTrue(entered.wait(2))
            second = pool.submit(self.request)
            try:
                second_result = second.result(timeout=2)
            finally:
                release.set()
            first_result = first.result(timeout=2)
        self.assertEqual(first_result["error"]["data"]["approval_id"], second_result["error"]["data"]["approval_id"])
        self.assertEqual(len(sent), 1)

    def test_expiry_boundary_and_hash_mismatch(self):
        aid, rec = self.pending()
        self.approve(aid, rec["code"])
        self.assertIn("error", self.request(CODE + ";", _approval_id=aid))
        with patch.object(self.g.time, "time", return_value=rec["expires"]):
            self.assertIn("error", self.request(_approval_id=aid))

    def test_invalid_json_shapes_rejected(self):
        cases = [("/approve", []), ("/approve", {"approval_id": []}),
                 ("/mcp", {"id": 1, "method": "tools/call", "params": []}),
                 ("/mcp", {"id": 1, "method": "tools/call", "params": {"name": [], "arguments": {}}}),
                 ("/mcp", {"id": 1, "method": "tools/call", "params": {"name": "repl", "arguments": {"code": []}}})]
        for path, body in cases:
            with self.subTest(body=body):
                status, _, response = self.t.http(path, body, "REQ", self.sid)
                self.assertTrue(status >= 400 or "error" in response)

    def test_body_limits_before_read(self):
        for length in ("-1", "invalid", str(2 ** 63)):
            with self.subTest(length=length):
                status, _, _ = self.t.http("/mcp", {}, "REQ", self.sid, headers={"Content-Length": length})
                self.assertGreaterEqual(status, 400)

    def test_ambiguous_framing_rejected_before_read(self):
        for framing in ([("Content-Length", "2"), ("Content-Length", "2")],
                        [("Content-Length", "2"), ("Transfer-Encoding", "chunked")]):
            h = object.__new__(self.g.Handler)
            h.headers = Message()
            for key, value in framing:
                h.headers[key] = value
            h.rfile = io.BytesIO(b"{}")
            with self.assertRaises(ValueError):
                h._read_json()
            self.assertEqual(h.rfile.tell(), 0)

    def test_concurrent_approve_only_one_transition(self):
        aid, rec = self.pending()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.approve(aid, rec["code"]), range(8)))
        self.assertEqual(results.count(200), 1)

    def test_telegram_error_does_not_leak_secrets(self):
        self.g._telegram_creds = lambda: ("FAKE_BOT_SECRET", "FAKE_CHAT")
        with patch("urllib.request.urlopen", side_effect=RuntimeError("FAKE_BOT_SECRET FAKE_APPROVAL_SECRET")):
            self.assertFalse(self.g.real_tg_send("FAKE_APPROVAL_SECRET"))
        self.assertNotIn("FAKE_BOT_SECRET", json.dumps(self.g.audit_entries))
        self.assertNotIn("FAKE_APPROVAL_SECRET", json.dumps(self.g.audit_entries))

    def test_malformed_json_fail_closed(self):
        for raw in (b"{", b"\xff", b'[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[['):
            self.assertEqual(self.t.http("/mcp", {}, "REQ", self.sid, raw=raw)[0], 400)

    def test_delivery_exception_fail_closed(self):
        self.g.tg_send = lambda _: (_ for _ in ()).throw(RuntimeError("fake transport failure"))
        response = self.request()
        self.assertIn("warning", response["error"]["data"])
        self.assertFalse(any(r["status"] in ("pending", "approved") for r in self.g.APPROVALS.values()))

    def test_secret_code_not_in_requester_visible_channels(self):
        aid, rec = self.pending()
        _, _, listing = self.t.http("/approvals", {}, "REQ", method="GET")
        visible = json.dumps([listing, self.g.audit_entries, self.request(_approval_id=aid)])
        self.assertNotIn(rec["code"], visible)
        self.assertFalse(self.g.notifications)


if __name__ == "__main__":
    unittest.main(verbosity=2)
