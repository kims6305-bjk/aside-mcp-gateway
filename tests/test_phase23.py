#!/usr/bin/env python3
"""Integration tests for gateway Phase 2/3 features against a running
gateway backed by the mock MCP server.

Usage: python3 test_phase23.py <base_url> <token> <outbox_dir>

Gateway config for this test: aside_bin = tests/mock_mcp.py,
gate.denylist_url_patterns = ["blocked\\.example"], telegram unset.

Note: the approval flow here only asserts the BLOCK side (contract:
code never reaches the requester, fail-closed without Telegram). The full
approve flow (approver role, one-shot, secret-path patterns) is covered
in-process by test_gate_selfapprove.py.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

BASE = sys.argv[1].rstrip("/")
TOKEN = sys.argv[2]
OUTBOX = sys.argv[3]
fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(extra)[:160]) if extra else ""))
    if not cond:
        fails.append(name)


def req(method, path, payload=None, sid=None, extra_headers=None, token=TOKEN):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        r.add_header("Content-Type", "application/json")
    if token is not None:
        r.add_header("Authorization", "Bearer " + token)
    if sid:
        r.add_header("Mcp-Session-Id", sid)
    for k, v in (extra_headers or {}).items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            body = resp.read().decode()
            return resp.status, dict(resp.headers), (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            parsed = json.loads(body) if body else None
        except Exception:
            parsed = None
        return e.code, dict(e.headers), parsed


def init_session(name=None):
    hdrs = {"X-Aside-Session": name} if name else None
    status, headers, resp = req("POST", "/mcp",
                                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2025-03-26",
                                            "capabilities": {},
                                            "clientInfo": {"name": "p23", "version": "1"}}},
                                extra_headers=hdrs)
    sid = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
    req("POST", "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid=sid)
    return sid, headers


def call(sid, rid, tool, args):
    return req("POST", "/mcp", {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                                "params": {"name": tool, "arguments": args}}, sid=sid)


def repl(sid, rid, code, title="t", **extra):
    args = {"title": title, "code": code}
    args.update(extra)
    return call(sid, rid, "repl", args)


def outbox_count():
    return len([f for f in os.listdir(OUTBOX) if f.endswith(".json")]) \
        if os.path.isdir(OUTBOX) else 0


# --- session + tools/list augmentation
sid, _ = init_session()
check("session created", bool(sid))
status, _, resp = req("POST", "/mcp", {"jsonrpc": "2.0", "id": 2,
                                       "method": "tools/list", "params": {}}, sid=sid)
names = [t["name"] for t in resp["result"]["tools"]]
check("tools/list augmented", "repl" in names and "aside_job_start" in names
      and "browser_list_tabs" in names, names)

# --- gate: read & write pass, destructive blocked
_, _, resp = repl(sid, 3, "const s = await snapshot(page); console.log(s.tree);")
check("read call passes", bool(resp.get("result")), json.dumps(resp)[:100])
_, _, resp = repl(sid, 4, "await page.locator('e1').click(); console.log('ok');")
check("write call passes (default mode)", bool(resp.get("result")), json.dumps(resp)[:100])

n_out = outbox_count()
_, _, resp = repl(sid, 5, "await fs.rm('/tmp/xx', {force:true}); console.log('gone');",
                  title="delete file")
# Self-approval fix: the approval code must NEVER reach the
# requesting agent's response. When Telegram delivery fails, the gate fails
# closed (no fallback code). The full approve flow is covered in-process by
# test_gate_selfapprove.py.
err = (resp or {}).get("error", {})
data = err.get("data", {}) if err else {}
check("destructive blocked -32010", err.get("code") == -32010
      and data.get("approval_required"), json.dumps(err)[:200])
aid = data.get("approval_id")
check("approval code NOT in response (self-approval fix)",
      "approval_code" not in data, json.dumps(data)[:200])
check("fail-closed warning when notify unavailable",
      "warning" in data or "Telegram" in (data.get("how") or ""),
      json.dumps(data)[:200])

# unapproved retry stays blocked
_, _, resp = repl(sid, 6, "await fs.rm('/tmp/xx', {force:true}); console.log('gone');",
                  title="delete file", _approval_id=aid, _approval="000000")
check("unapproved retry stays blocked", bool((resp or {}).get("error")))

# denylist hard block
_, _, resp = repl(sid, 10, "const p = await openTab('https://blocked.example/x');",
                  title="deny test")
err = (resp or {}).get("error", {})
check("denylist blocks -32011", err.get("code") == -32011, json.dumps(err)[:140])

# --- named session resume
sid_a, h_a = init_session("demo")
sid_b, h_b = init_session("demo")
resumed = (h_b.get("X-Aside-Session-Resumed") or h_b.get("x-aside-session-resumed"))
check("named session resumed", sid_a == sid_b and resumed == "true",
      f"same_sid={sid_a == sid_b} resumed={resumed}")

# --- jobs
_, _, resp = call(sid, 20, "aside_job_start",
                  {"title": "echo job", "code": "console.log('job-hello');",
                   "notify": True})
txt = resp["result"]["content"][0]["text"]
job_id = json.loads(txt)["job_id"]
check("job started", bool(job_id), txt[:100])
status_done = None
for _ in range(30):
    _, _, resp = call(sid, 21, "aside_job_status", {"job_id": job_id})
    st = json.loads(resp["result"]["content"][0]["text"])
    if st["status"] != "running":
        status_done = st["status"]
        break
    time.sleep(0.5)
check("job finished done", status_done == "done", status_done)
_, _, resp = call(sid, 22, "aside_job_result", {"job_id": job_id})
check("job result has content", bool((resp or {}).get("result", {}).get("content")))

_, _, resp = call(sid, 23, "aside_job_start",
                  {"title": "bad job", "code": "await fs.rm('/x');", "notify": False})
txt = resp["result"]["content"][0]["text"]
check("destructive job refused", "destructive" in txt, txt[:120])

# --- preview cache + endpoint + synthesized tool
_, _, resp = repl(sid, 30, "MAKE_IMAGE please", title="shot")
check("image call passes", bool((resp or {}).get("result")))
_, _, resp = call(sid, 31, "aside_preview_latest", {})
kinds = [b.get("type") for b in (resp or {}).get("result", {}).get("content", [])]
check("preview_latest returns image", "image" in kinds, kinds)
try:
    r2 = urllib.request.Request(BASE + "/preview.jpg")
    r2.add_header("Authorization", "Bearer " + TOKEN)
    with urllib.request.urlopen(r2, timeout=60) as resp2:
        raw2 = resp2.read()
        ct2 = resp2.headers.get("Content-Type", "")
    status = resp2.status
    ok = status == 200 and raw2[:4] == b"\x89PNG" and "png" in ct2
except Exception as e:
    ok, status = False, f"err {e}"
check("GET /preview.jpg 200 image/png", ok, f"status={status}")
_, _, resp = call(sid, 32, "browser_list_tabs", {})
txt = " ".join(b.get("text", "") for b in (resp or {}).get("result", {}).get("content", []))
check("browser_list_tabs synthesized", "listBrowserTabs" in txt, txt[:100])

print("RESULT:", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(0 if not fails else 1)
