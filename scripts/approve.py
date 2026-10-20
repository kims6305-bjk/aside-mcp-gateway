#!/usr/bin/env python3
"""Approve a pending aside-mcp-gateway action with the 6-digit code from Telegram.

Run this as the *operator* (or an approver bot acting on the operator's reply),
never from the agent that made the request.

  export ASIDE_GW_URL=http://<gateway-host>:11441
  export ASIDE_APPROVER_TOKEN=<token with role "approver">   # or ASIDE_APPROVER_TOKEN_FILE
  python3 scripts/approve.py 123456

The code is tried against every pending approval; the gateway rejects wrong
codes and approvals by the requesting client itself. Exit 0 = approved.
"""
import json
import os
import sys
import urllib.error
import urllib.request

if len(sys.argv) != 2 or not sys.argv[1].strip().isdigit():
    sys.exit(__doc__)
code = sys.argv[1].strip()
base = os.environ.get("ASIDE_GW_URL", "http://127.0.0.1:11441").rstrip("/")
tok = os.environ.get("ASIDE_APPROVER_TOKEN") or open(
    os.path.expanduser(os.environ["ASIDE_APPROVER_TOKEN_FILE"])).read().strip()
hdr = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}


def call(path, body=None):
    req = urllib.request.Request(base + path, headers=hdr,
                                 data=json.dumps(body).encode() if body else None)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, {}


status, listing = call("/approvals")
if status != 200:
    sys.exit("cannot list approvals: HTTP %d (check URL / approver token)" % status)
pending = [p for p in listing.get("pending", []) if p["status"] == "pending"]
for p in pending:
    if call("/approve", {"approval_id": p["id"], "code": code})[0] == 200:
        print("approved %s · %s · %s — the agent can now retry" % (p["id"], p["client"], p["title"]))
        sys.exit(0)
print("no match among %d pending approval(s): wrong code, expired, or already used" % len(pending))
sys.exit(1)
