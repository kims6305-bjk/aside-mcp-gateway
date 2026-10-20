#!/usr/bin/env python3
"""Minimal fake `aside mcp` stdio server for local gateway tests."""
import json
import sys

TOOLS = [{
    "name": "repl",
    "description": "mock repl",
    "inputSchema": {"type": "object",
                    "properties": {"title": {"type": "string"},
                                   "code": {"type": "string"}},
                    "required": ["title", "code"]},
}]

TINY_PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
                "Xw8AAwMCAO+ip1sAAAAASUVORK5CYII=")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        method = msg.get("method")
        rid = msg.get("id")
        if method == "initialize":
            resp = {"protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {"listChanged": True}},
                    "serverInfo": {"name": "mock-aside", "version": "0.0"}}
        elif method == "tools/list":
            resp = {"tools": TOOLS}
        elif method == "tools/call":
            code = (msg.get("params") or {}).get("arguments", {}).get("code", "")
            if "MAKE_IMAGE" in code:
                resp = {"content": [
                    {"type": "text", "text": "Displayed image (mock)"},
                    {"type": "image", "data": TINY_PNG_B64, "mimeType": "image/png"},
                ], "isError": False}
            else:
                resp = {"content": [{"type": "text", "text": "echo: " + code}],
                        "isError": False}
        elif rid is None:
            continue  # notification
        else:
            resp = {}
        if rid is not None:
            sys.stdout.write(json.dumps(
                {"jsonrpc": "2.0", "id": rid, "result": resp}) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
