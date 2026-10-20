#!/usr/bin/env python3
"""One-shot CLI client for aside-mcp-gateway.

Runs one or more `repl` code steps in a fresh MCP session and prints results.
Text blocks go to stdout; image blocks are saved to files (paths printed).

Env:
  ASIDE_GW_URL    gateway base URL (default http://127.0.0.1:11441)
  ASIDE_GW_TOKEN  bearer token (required)
  ASIDE_GW_PROXY  optional HTTP proxy (e.g. a tailnet proxy)
  ASIDE_GW_OUT    image output dir (default ./gw-images)

Usage:
  gateway_client.py --title "List tabs" --code "console.log(JSON.stringify(await listBrowserTabs()))"
  gateway_client.py --title "Read page" --code "..." --code "..."   # steps share one session
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.request
import urllib.error

BASE = os.environ.get("ASIDE_GW_URL", "http://127.0.0.1:11441").rstrip("/")
TOKEN = os.environ.get("ASIDE_GW_TOKEN", "")
PROXY = os.environ.get("ASIDE_GW_PROXY")
OUT_DIR = os.environ.get("ASIDE_GW_OUT", "./gw-images")

if PROXY:
    urllib.request.install_opener(urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})))


def post(payload, sid=None, method="POST"):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + "/mcp", data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", "Bearer " + TOKEN)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="task")
    ap.add_argument("--code", action="append", required=True,
                    help="JS code for the repl tool; repeat for multi-step flows")
    args = ap.parse_args()
    if not TOKEN:
        print("ASIDE_GW_TOKEN is not set", file=sys.stderr)
        return 2

    status, headers, resp = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2025-03-26",
                                             "capabilities": {},
                                             "clientInfo": {"name": "gateway-client",
                                                            "version": "1.0"}}})
    sid = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
    if status != 200 or not sid:
        print(f"initialize failed: status={status} resp={resp}", file=sys.stderr)
        return 1
    post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid=sid)

    rc = 0
    try:
        for i, code in enumerate(args.code, 1):
            status, _, resp = post({"jsonrpc": "2.0", "id": 10 + i,
                                    "method": "tools/call",
                                    "params": {"name": "repl",
                                               "arguments": {"title": args.title,
                                                             "code": code}}},
                                   sid=sid)
            result = (resp or {}).get("result")
            if result is None:
                print(f"[step {i}] RPC error: {json.dumps(resp, ensure_ascii=False)}",
                      file=sys.stderr)
                rc = 1
                continue
            for block in result.get("content", []):
                kind = block.get("type")
                if kind == "text":
                    print(block.get("text", ""))
                elif kind == "image":
                    os.makedirs(OUT_DIR, exist_ok=True)
                    ext = "jpg" if "jpeg" in (block.get("mimeType") or "") else "png"
                    path = os.path.join(
                        OUT_DIR, f"gw-{time.strftime('%H%M%S')}-{i}.{ext}")
                    with open(path, "wb") as f:
                        f.write(base64.b64decode(block.get("data", "")))
                    print(f"[image saved: {path}]")
                else:
                    print(f"[{kind} block]")
            if result.get("isError"):
                rc = 1
    finally:
        try:
            post(None, sid=sid, method="DELETE")
        except Exception:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
