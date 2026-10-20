#!/usr/bin/env python3
"""E2E test client for aside-mcp-gateway over HTTP.

Usage: python3 test_http.py <base_url> <token> [--with-browser]
--with-browser runs real browser steps (open example.com, screenshot, close).
Without it, runs a harmless listBrowserTabs call only.
Prints PASS/FAIL lines; exit code 0 iff all checks pass.
"""
import json
import os
import sys
import urllib.request
import urllib.error

BASE = sys.argv[1].rstrip("/")
TOKEN = sys.argv[2]
WITH_BROWSER = "--with-browser" in sys.argv
fails = []

# Optional HTTP proxy.
_PROXY = os.environ.get("ASIDE_GW_PROXY")
if _PROXY:
    urllib.request.install_opener(urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": _PROXY, "https": _PROXY})))


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + extra) if extra else ""))
    if not cond:
        fails.append(name)


def post(payload, token=TOKEN, sid=None, expect=None):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + "/mcp", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token is not None:
        req.add_header("Authorization", "Bearer " + token)
    if sid:
        req.add_header("Mcp-Session-Id", sid)
    try:
        with urllib.request.urlopen(req, timeout=200) as r:
            body = r.read().decode()
            return r.status, dict(r.headers), (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            parsed = json.loads(body) if body else None
        except Exception:
            parsed = None
        return e.code, dict(e.headers), parsed


def call_tool(sid, rid, title, code):
    status, _, resp = post({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                            "params": {"name": "repl",
                                       "arguments": {"title": title, "code": code}}},
                           sid=sid)
    return status, resp


# 1. no token -> 401
status, _, _ = post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token=None)
check("no-token rejected (401)", status == 401, f"status={status}")

# 2. healthz
try:
    with urllib.request.urlopen(BASE + "/healthz", timeout=10) as r:
        health = json.loads(r.read().decode())
    check("healthz ok", health.get("ok") is True, json.dumps(health)[:120])
except Exception as e:
    check("healthz ok", False, str(e))

# 3. initialize -> session id
status, headers, resp = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-03-26",
                                         "capabilities": {},
                                         "clientInfo": {"name": "phase1-test",
                                                        "version": "0.1"}}})
sid = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
check("initialize 200 + session id", status == 200 and bool(sid),
      f"status={status} sid={'yes' if sid else 'no'}")
check("initialize serverInfo", bool(resp and resp.get("result", {}).get("serverInfo")),
      json.dumps((resp or {}).get("result", {}).get("serverInfo", {})))

# 4. notification -> 202
status, _, _ = post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid=sid)
check("notification accepted (202)", status == 202, f"status={status}")

# 5. bad session -> rpc error
status, _, resp = post({"jsonrpc": "2.0", "id": 9, "method": "tools/list"},
                       sid="deadbeef" * 4)
check("bad session rejected", bool(resp and resp.get("error")),
      json.dumps(resp)[:120] if resp else f"status={status}")

# 6. tools/list -> repl only
status, _, resp = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                       sid=sid)
tools = (resp or {}).get("result", {}).get("tools", [])
names = [t.get("name") for t in tools]
check("tools/list has repl + gateway tools",
      "repl" in names and "aside_job_start" in names, f"tools={names}")

# 7. harmless call
status, resp = call_tool(sid, 3, "List tabs (test)",
                         "const tl = await listBrowserTabs(); console.log('tabs=' + tl.length);")
content = (resp or {}).get("result", {}).get("content", [])
text = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
check("tools/call listBrowserTabs", "tabs=" in text, text[:120])

if WITH_BROWSER:
    status, resp = call_tool(sid, 4, "Open example (test)",
                             "const pg = await openTab('https://example.com'); console.log('opened:' + pg.url());")
    content = (resp or {}).get("result", {}).get("content", [])
    text = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
    check("openTab example.com", "opened:https://example.com" in text, text[:120])

    status, resp = call_tool(sid, 5, "Screenshot (test)",
                             "const sh = await page.screenshot({type:'png'}); display(sh); console.log('shot-done');")
    content = (resp or {}).get("result", {}).get("content", [])
    kinds = [c.get("type") for c in content]
    check("screenshot returns image block", "image" in kinds, f"kinds={kinds}")

    status, resp = call_tool(sid, 6, "Close tab (test)",
                             "await closeTab(page); console.log('closed');")
    content = (resp or {}).get("result", {}).get("content", [])
    text = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
    check("closeTab restores state", "closed" in text, text[:80])

# 8. DELETE session
req = urllib.request.Request(BASE + "/mcp", method="DELETE")
req.add_header("Authorization", "Bearer " + TOKEN)
req.add_header("Mcp-Session-Id", sid)
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        check("DELETE session 200", r.status == 200)
except Exception as e:
    check("DELETE session 200", False, str(e))

print("RESULT:", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(0 if not fails else 1)
