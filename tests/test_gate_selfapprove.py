#!/usr/bin/env python3
"""Self-approval regression test for gateway.py (in-process, mock aside, fake Telegram).
Run: /usr/bin/python3 test_gate_selfapprove.py  -> prints PASS lines, exit 0 iff all pass."""
import json, os, shutil, stat, sys, tempfile, threading, urllib.request, urllib.error
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
MOCK = os.path.join(HERE, "mock_mcp.py")
tmp = tempfile.mkdtemp()
shutil.copy(os.path.join(HERE, "..", "gateway.py"), tmp)
mock = os.path.join(tmp, "mock_aside"); shutil.copy(MOCK, mock)
os.chmod(mock, os.stat(mock).st_mode | stat.S_IEXEC)
json.dump({"aside_bin": mock, "host": "127.0.0.1", "port": 0, "log_dir": os.path.join(tmp, "logs"),
           "tokens_file": os.path.join(tmp, "tokens.json")}, open(os.path.join(tmp, "config.json"), "w"))
json.dump({"tokens": {"REQ": "main-client", "APP": {"name": "approver", "role": "approver"}}},
          open(os.path.join(tmp, "tokens.json"), "w"))
sys.path.insert(0, tmp)
import gateway as g

SENT, TG_OK = [], [True]
g.tg_send = lambda text: (SENT.append(text), TG_OK[0])[1]
g.notify_user = lambda *a, **k: "outbox-only"  # no real GitHub issues from tests
srv = ThreadingHTTPServer(("127.0.0.1", 0), g.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % srv.server_address[1]
fails = []

def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (" | " + str(extra)[:200] if extra and not cond else ""))
    if not cond: fails.append(name)

def http(path, body, tok, sid=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json"); req.add_header("Authorization", "Bearer " + tok)
    if sid: req.add_header("Mcp-Session-Id", sid)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read(); return r.status, dict(r.headers), json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read(); return e.code, dict(e.headers), json.loads(raw) if raw else {}

st, h, _ = http("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}, "REQ")
sid = h.get("Mcp-Session-Id") or h.get("mcp-session-id")
http("/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"}, "REQ", sid)
CODE = "await fs.rm(pwd + '/tmp/gate-fix-test.txt'); console.log('deleted')"
def call(args, rid=2):
    a = {"title": "del", "code": CODE}; a.update(args)
    return http("/mcp", {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                         "params": {"name": "repl", "arguments": a}}, "REQ", sid)

st, _, r = call({})
err = r.get("error") or {}
aid = (err.get("data") or {}).get("approval_id")
check("destructive call blocked", err.get("code") == -32010 and aid, r)
check("code NOT in requester response", "approval_code" not in json.dumps(r) and not any(c.isdigit() and len(c) == 6 for c in json.dumps(r).split('"')), r)
check("code delivered to Telegram", len(SENT) == 1 and "Approval code" in SENT[0], SENT)
acode = SENT[0].split("Approval code: ")[1][:6]
check("code not in outbox/audit", acode not in "".join(open(os.path.join(tmp, "logs", f)).read() for f in os.listdir(os.path.join(tmp, "logs"))))

st, _, r = http("/approve", {"approval_id": aid, "code": acode}, "REQ")
check("requester self-approve via /approve -> 403", st == 403, (st, r))
st, _, r = call({"_approval_id": aid}, 3)
check("still blocked after failed self-approve", (r.get("error") or {}).get("code") == -32010, r)
st, _, r = http("/approve", {"approval_id": aid, "code": "000000" if acode != "000000" else "111111"}, "APP")
check("approver with wrong code -> 403", st == 403, (st, r))
st, _, r = http("/mcp", {"jsonrpc": "2.0", "id": 9, "method": "tools/list"}, "APP", sid)
check("approver token cannot call /mcp", st == 403, (st, r))
st, _, r = http("/approve", {"approval_id": aid, "code": acode}, "APP")
check("approver with right code -> 200", st == 200, (st, r))
st, _, r = call({"_approval_id": aid}, 4)
check("requester retry executes after approval", "echo:" in json.dumps(r, ensure_ascii=False), r)
st, _, r = call({"_approval_id": aid}, 5)
check("approval is one-shot", (r.get("error") or {}).get("code") == -32010, r)

lv, why = g.classify_code("const t = await fs.readFile('/home/user/.ssh/id_ed25519','utf8')")
check("secret path read needs approval", lv == "destructive" and "secret path access" in why, (lv, why))

TG_OK[0] = False; SENT.clear()
st, _, r = call({"code": CODE + " // 2"}, 6)
d = (r.get("error") or {}).get("data") or {}
check("telegram down -> fail closed, no code", "approval_code" not in d and "warning" in d, r)
srv.shutdown()
print("ALL PASS" if not fails else "FAILED: %s" % fails)
sys.exit(1 if fails else 0)
