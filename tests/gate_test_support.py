"""Socket-free handler transport; no real config, credentials, child or network.

Expected security outcomes in tests derive from the user's approval invariant.
Fixture values and iteration counts are test inputs, not production estimates.
"""
import io
import json
import os
import subprocess
import types
import urllib.request
from email.message import Message
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))


def forbidden(*args, **kwargs):
    raise AssertionError("external IO forbidden in gate tests")


def load_gateway():
    path = os.path.join(HERE, "..", "gateway.py")
    with open(path, encoding="utf-8") as source:
        code = compile(source.read(), path, "exec")
    g = types.ModuleType("gateway_under_test")
    g.__file__ = path
    # Do not open even a real config file during module initialization.
    with patch("builtins.open", side_effect=forbidden), patch("os.makedirs"):
        exec(code, g.__dict__)
    g.audit_entries = []
    g.audit = lambda entry: g.audit_entries.append(dict(entry))
    g.sent = []
    g.tg_ok = True
    g.real_tg_send = g.tg_send
    g.tg_send = lambda text: (g.sent.append(text), g.tg_ok)[1]
    g.notifications = []
    g.notify_user = lambda *a, **kw: (g.notifications.append((a, kw)), "outbox-only")[1]
    g._telegram_creds = forbidden
    g.clients = {
        "REQ": {"name": "agent-main", "gate": "approve-destructive"},
        "OTHER": {"name": "other", "gate": "approve-destructive"},
        "APP": {"name": "approver-bot", "role": "approver"},
    }
    g.load_clients = lambda: g.clients

    class FakeChild:
        def __init__(self, client_name):
            self.client_name = client_name
            self.alive = True
            self.last_used = 0
            self.init_resp = None
            self.calls = []

        def request(self, obj, **kwargs):
            self.calls.append(obj)
            if "id" not in obj:
                return None
            return {"jsonrpc": "2.0", "id": obj["id"],
                    "result": g.text_result("echo:" + json.dumps(obj))}

        def call_repl(self, title, code, rid=99):
            return self.request({"id": rid, "method": "tools/call",
                                 "params": {"name": "repl", "arguments":
                                            {"title": title, "code": code}}})

        def terminate(self):
            self.alive = False

    g.ChildSession = FakeChild
    return g


class Transport:
    def __init__(self, g):
        self.g = g

    def http(self, path, body, tok, sid=None, method="POST", raw=None, headers=None):
        g = self.g

        class MemoryHandler(g.Handler):
            def send_response(self, status, message=None):
                self.status = status

            def send_header(self, key, value):
                self.response_headers[key] = value

            def end_headers(self):
                pass

        h = object.__new__(MemoryHandler)
        h.path = path
        h.headers = Message()
        h.headers["Authorization"] = "Bearer " + tok
        if sid:
            h.headers["Mcp-Session-Id"] = sid
        data = json.dumps(body).encode() if raw is None else raw
        h.headers["Content-Length"] = str(len(data))
        for key, value in (headers or {}).items():
            h.headers.replace_header(key, value) if key in h.headers else h.headers.add_header(key, value)
        h.rfile, h.wfile = io.BytesIO(data), io.BytesIO()
        h.response_headers = {}
        # A missed mock must fail before any external side effect.
        with patch.object(subprocess, "Popen", side_effect=forbidden), \
                patch.object(subprocess, "run", side_effect=forbidden), \
                patch.object(urllib.request, "urlopen", side_effect=forbidden):
            getattr(h, "do_" + method)()
        response = h.wfile.getvalue()
        return h.status, h.response_headers, json.loads(response) if response else {}

    def initialize(self, tok="REQ"):
        _, headers, _ = self.http("/mcp", {"id": 1, "method": "initialize"}, tok)
        return headers["Mcp-Session-Id"]

    def call(self, sid, args, tok="REQ", tool="repl", rid=2):
        return self.http("/mcp", {"jsonrpc": "2.0", "id": rid,
                                 "method": "tools/call", "params":
                                 {"name": tool, "arguments": args}}, tok, sid)
