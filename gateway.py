#!/usr/bin/env python3
"""aside-mcp-gateway — Phase 1+2+3

Wraps the official `aside mcp` stdio server and exposes it as a
Streamable-HTTP MCP endpoint on a private-network bind address (e.g. Tailscale).

Phase 1: stdio<->HTTP proxy, one `aside mcp` child per MCP session,
         per-client Bearer tokens, /healthz, audit log.
Phase 2: named sessions (X-Aside-Session header resumes the same child
         process / REPL scope), async jobs (aside_job_start/status/result
         gateway tools running on dedicated child processes), completion
         notifications via an optional GitHub-issue bridge with an
         outbox-file fallback.
Phase 3: safety gate — every `repl` call's code is classified
         read/write/destructive by pattern analysis; destructive calls
         need a one-time approval code delivered out-of-band (Telegram DM
         only; POST /approve or retry with _approval). URL denylist hard
         blocks. Preview cache: the latest screenshot from any session is
         kept and served via aside_preview_latest / GET /preview.jpg.
         One synthesized read-only tool: browser_list_tabs.

stdlib only. Python 3.9+.
"""
import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


CONFIG = load_json(os.path.join(BASE_DIR, "config.json"), {}) or {}
HOST = CONFIG.get("host", "127.0.0.1")
PORT = int(CONFIG.get("port", 11441))
ASIDE_BIN = CONFIG.get("aside_bin", os.path.expanduser("~/.local/bin/aside"))
TOKENS_FILE = CONFIG.get("tokens_file", os.path.join(BASE_DIR, "tokens.json"))
IDLE_TIMEOUT_SEC = int(CONFIG.get("idle_timeout_sec", 2100))
REQUEST_TIMEOUT_SEC = int(CONFIG.get("request_timeout_sec", 180))
LOG_DIR = CONFIG.get("log_dir", os.path.join(BASE_DIR, "logs"))
OUTBOX_DIR = os.path.join(BASE_DIR, "outbox")
STARTED_AT = time.time()

GATE_CFG = CONFIG.get("gate", {}) or {}
GATE_DEFAULT_MODE = GATE_CFG.get("default_mode", "approve-destructive")
APPROVAL_TTL_SEC = int(GATE_CFG.get("approval_ttl_sec", 600))
DENYLIST_PATTERNS = [re.compile(p, re.I) for p in GATE_CFG.get("denylist_url_patterns", [])]
NOTIFY_CFG = CONFIG.get("notify", {}) or {}
BRIDGE_REPO = NOTIFY_CFG.get("bridge_repo", "")  # "owner/repo" to enable; empty = outbox only
BRIDGE_LABEL = NOTIFY_CFG.get("bridge_label", "agent-task")
JOB_NOTIFY_DEFAULT = bool(NOTIFY_CFG.get("job_notify_default", True))

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(OUTBOX_DIR, exist_ok=True)
AUDIT_PATH = os.path.join(LOG_DIR, "audit.jsonl")
_audit_lock = threading.Lock()


def audit(entry):
    entry = dict(entry)
    entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        with _audit_lock:
            with open(AUDIT_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def load_clients():
    """tokens.json: {"tokens": {"<token>": "name" | {"name":..., "gate":...}}}"""
    data = load_json(TOKENS_FILE, {}) or {}
    out = {}
    for token, v in (data.get("tokens", {}) or {}).items():
        if isinstance(v, str):
            out[token] = {"name": v, "gate": GATE_DEFAULT_MODE}
        elif isinstance(v, dict):
            out[token] = {"name": v.get("name", "client"),
                          "gate": v.get("gate", GATE_DEFAULT_MODE),
                          "role": v.get("role", "client")}
    return out


# ---------------------------------------------------------------- notify

def find_gh():
    for cand in (shutil.which("gh"), "/opt/homebrew/bin/gh",
                 "/usr/local/bin/gh", "/usr/bin/gh"):
        if cand and os.path.exists(cand):
            return cand
    return None


def notify_user(title, body):
    """Best-effort user notification: an issue on the optional bridge repo
    (some other agent may forward it). Always also drops an outbox file.
    Returns 'gh-issue' | 'outbox-only'."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    fname = os.path.join(OUTBOX_DIR, f"{stamp}-{uuid.uuid4().hex[:6]}.json")
    try:
        with open(fname, "w", encoding="utf-8") as f:
            json.dump({"title": title, "body": body, "ts": stamp}, f,
                      ensure_ascii=False)
    except Exception:
        pass
    gh = find_gh()
    if gh and BRIDGE_REPO:
        try:
            r = subprocess.run(
                [gh, "issue", "create", "--repo", BRIDGE_REPO,
                 "--label", BRIDGE_LABEL, "--title", title, "--body", body],
                capture_output=True, text=True, timeout=40)
            if r.returncode == 0:
                audit({"event": "notify_sent", "via": "gh-issue", "title": title})
                return "gh-issue"
            audit({"event": "notify_gh_failed", "err": (r.stderr or "")[:200]})
        except Exception as e:
            audit({"event": "notify_gh_failed", "err": str(e)[:200]})
    audit({"event": "notify_sent", "via": "outbox-only", "title": title})
    return "outbox-only"


def _telegram_creds():
    """Return (bot_token, chat_id) from config.
    config.json "telegram" section supports:
      {"env_file": "~/.config/aside-gateway-telegram.env"}   # KEY=VALUE file
      {"bot_token": "...", "chat_id": "..."}                  # inline (keep config.json private!)
    Returns (None, None) when nothing is configured."""
    tg = CONFIG.get("telegram") or {}
    env_file = tg.get("env_file")
    if env_file:
        try:
            env = dict(l.split("=", 1)
                       for l in open(os.path.expanduser(env_file), encoding="utf-8")
                       .read().splitlines()
                       if "=" in l and not l.startswith("#"))
            return env.get("TELEGRAM_BOT_TOKEN", "").strip() or None, \
                env.get("TELEGRAM_HOME_CHANNEL", "").strip() or None
        except OSError:
            pass
    if tg.get("bot_token") and tg.get("chat_id"):
        return tg["bot_token"].strip(), str(tg["chat_id"]).strip()
    return None, None


def tg_send(text):
    """SELF-APPROVAL FIX: approval codes go ONLY to the user's Telegram DM.
    GitHub issues, the outbox and RPC responses are readable by the requesting
    agent, so they must never carry the code. Returns True on delivery."""
    import urllib.parse, urllib.request
    try:
        bot_token, chat_id = _telegram_creds()
        if not bot_token or not chat_id:
            raise RuntimeError("telegram not configured")
        data = urllib.parse.urlencode({"chat_id": chat_id,
                                       "text": text[:3900]}).encode()
        url = "https://api.telegram.org/bot" + bot_token + "/sendMessage"
        with urllib.request.urlopen(url, data=data, timeout=20) as r:
            ok = bool(json.load(r).get("ok"))
    except Exception as e:  # never log the URL (contains the bot token)
        audit({"event": "tg_send_failed", "err": type(e).__name__})
        return False
    audit({"event": "notify_sent", "via": "telegram-direct"})
    return ok


# ---------------------------------------------------------------- safety gate

_DESTRUCTIVE_PATTERNS = [
    (r"fs\.(rm|unlink|rmdir)\b", "fs delete"),
    (r"\beval\s*\(", "eval"),
    # ponytail: literal-path match only; string-built paths evade it. Real fix = run aside under a separate OS user.
    (r"aside-mcp-gateway|tokens\.json|\.env\b|\.ssh|\.aws|\.gnupg|Keychains|telegram\.env", "secret path access"),
    (r"new\s+Function\s*\(", "dynamic function"),
    (r"\.submit\s*\(|requestSubmit\s*\(", "form submit"),
    (r"fetch\s*\([^)]*method\s*:\s*['\"](POST|PUT|DELETE|PATCH)['\"]",
     "fetch with write method"),
    (r"결제|주문하기|구매하기|송금|이체하기|회원\s*탈퇴|계정\s*삭제", "payment/destructive keyword"),
    (r"checkout|place\s*order|buy\s*now|pay\s*now|delete\s*account", "payment/destructive keyword"),
    (r"전송하기|발송하기|게시하기|제출하기|보내기\s*['\"]?\s*\)", "send/publish/submit keyword"),
]
_WRITE_PATTERNS = [
    r"\.click\s*\(", r"\.fill\s*\(", r"\.type\s*\(", r"\.press\s*\(",
    r"\.goto\s*\(", r"openTab\s*\(", r"closeTab\s*\(", r"attachBrowserTab",
    r"attachActiveBrowserTab", r"fs\.writeFile", r"fs\.appendFile",
    r"fs\.mkdir", r"fetch\s*\(", r"\.selectOption\s*\(", r"\.check\s*\(",
    r"\.uncheck\s*\(", r"\.dblclick\s*\(", r"mouse\.", r"keyboard\.",
    r"dragAndDrop", r"\.hover\s*\(", r"page\.pdf\(",
]
_READ_PATTERNS = [
    r"snapshot\s*\(", r"listBrowserTabs", r"console\.log", r"display\s*\(",
    r"screenshot\s*\(", r"annotatedScreenshot", r"\.url\s*\(", r"\.title\s*\(",
    r"textContent", r"innerText", r"getAttribute", r"waitForSelector",
    r"sleep\s*\(", r"fs\.readFile", r"\.count\s*\(",
]
_URL_RE = re.compile(r"https?://[^\s'\"`\)\]]+")


def classify_code(code):
    """Returns (level, reasons). Levels: read | write | destructive."""
    reasons = []
    for pat, why in _DESTRUCTIVE_PATTERNS:
        if re.search(pat, code, re.I):
            reasons.append(why)
    if reasons:
        return "destructive", reasons
    if any(re.search(p, code) for p in _WRITE_PATTERNS):
        return "write", ["side-effect api"]
    if any(re.search(p, code) for p in _READ_PATTERNS):
        return "read", ["read-only api"]
    return "write", ["unclassified -> write"]


def denylist_hit(code):
    urls = _URL_RE.findall(code or "")
    for u in urls:
        for pat in DENYLIST_PATTERNS:
            if pat.search(u):
                return u
    return None


# approvals: approval_id -> record
APPROVALS = {}
APPROVALS_LOCK = threading.Lock()


def new_approval(client_name, code, title):
    aid = uuid.uuid4().hex[:12]
    acode = f"{secrets.randbelow(1000000):06d}"
    rec = {
        "id": aid, "code": acode, "client": client_name,
        "code_sha": hashlib.sha256((code or "").encode()).hexdigest(),
        "title": title or "",
        "preview": (code or "")[:200],
        "status": "pending",
        "expires": time.time() + APPROVAL_TTL_SEC,
    }
    with APPROVALS_LOCK:
        APPROVALS[aid] = rec
    return rec


def approval_state(rec, client_name, code):
    """-> 'ok' (consume) | 'pending' | 'mismatch' | 'expired'"""
    if rec is None:
        return "mismatch"
    if time.time() > rec["expires"]:
        rec["status"] = "expired"
        return "expired"
    if rec["client"] != client_name:
        return "mismatch"
    if rec["code_sha"] != hashlib.sha256((code or "").encode()).hexdigest():
        return "mismatch"
    if rec["status"] == "approved":
        rec["status"] = "consumed"
        return "ok"
    return "pending"


# ---------------------------------------------------------------- child proc

class ChildDead(Exception):
    pass


class ChildSession:
    """One `aside mcp` process + its pending-request dispatch."""

    def __init__(self, client_name):
        self.client_name = client_name
        self.created = time.time()
        self.last_used = time.time()
        self.alive = True
        self.init_resp = None
        self._lock = threading.Lock()
        self._pending = {}
        self._pending_lock = threading.Lock()
        self.proc = subprocess.Popen(
            [ASIDE_BIN, "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=open(os.path.join(LOG_DIR, "aside-mcp-stderr.log"), "ab"),
        )
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        try:
            for raw in self.proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                mid = msg.get("id")
                if mid is not None and ("result" in msg or "error" in msg):
                    with self._pending_lock:
                        q = self._pending.pop(mid, None)
                    if q is not None:
                        q.put(msg)
                elif msg.get("method"):
                    audit({"event": "server_notification",
                           "method": msg.get("method"),
                           "client": self.client_name})
        except Exception:
            pass
        self.alive = False
        with self._pending_lock:
            pending, self._pending = self._pending, {}
        for q in pending.values():
            q.put(None)

    def request(self, obj, timeout=REQUEST_TIMEOUT_SEC):
        with self._lock:
            if not self.alive:
                raise ChildDead("aside mcp process is not running")
            self.last_used = time.time()
            rid = obj.get("id")
            q = None
            if rid is not None:
                q = queue.Queue(maxsize=1)
                with self._pending_lock:
                    self._pending[rid] = q
            try:
                self.proc.stdin.write((json.dumps(obj) + "\n").encode())
                self.proc.stdin.flush()
            except Exception as e:
                self.alive = False
                raise ChildDead(str(e))
            if q is None:
                return None
            try:
                resp = q.get(timeout=timeout)
            except queue.Empty:
                with self._pending_lock:
                    self._pending.pop(rid, None)
                raise TimeoutError(f"no response within {timeout}s")
            if resp is None:
                raise ChildDead("aside mcp process exited")
            self.last_used = time.time()
            return resp

    def handshake(self):
        resp = self.request({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2025-03-26",
                                        "capabilities": {},
                                        "clientInfo": {"name": "aside-gateway",
                                                       "version": "2.0"}}})
        self.init_resp = resp
        self.request({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return resp

    def call_repl(self, title, code, rid=99):
        return self.request({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                             "params": {"name": "repl",
                                        "arguments": {"title": title,
                                                      "code": code}}})

    def terminate(self):
        self.alive = False
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


SESSIONS = {}       # sid -> {"child", "name", "client"}
NAMED = {}          # (client, name) -> sid
SESSIONS_LOCK = threading.Lock()

# jobs: job_id -> record
JOBS = {}
JOBS_LOCK = threading.Lock()

# preview cache
PREVIEW = {"latest": None}   # {"ts", "client", "data", "mimeType"}
PREVIEW_LOCK = threading.Lock()


def cache_preview(client_name, result):
    if not isinstance(result, dict):
        return
    for block in result.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "image" and block.get("data"):
            with PREVIEW_LOCK:
                PREVIEW["latest"] = {
                    "ts": time.time(), "client": client_name,
                    "data": block["data"],
                    "mimeType": block.get("mimeType", "image/jpeg"),
                }


def run_job(job_id):
    with JOBS_LOCK:
        job = JOBS[job_id]
    child = ChildSession(f"job:{job['client']}")
    try:
        child.handshake()
        resp = child.call_repl(job["title"], job["code"], rid=7)
        result = (resp or {}).get("result")
        if result is None:
            job.update(status="error", finished=time.time(),
                       error=json.dumps((resp or {}).get("error", {}))[:500])
        else:
            cache_preview(job["client"], result)
            job.update(status="done", finished=time.time(), result=result)
    except Exception as e:
        job.update(status="error", finished=time.time(), error=str(e)[:500])
    finally:
        child.terminate()
    audit({"event": "job_finished", "job": job_id, "status": job["status"],
           "client": job["client"], "title": job["title"]})
    if job.get("notify"):
        first_text = ""
        if job.get("result"):
            for b in job["result"].get("content", []):
                if b.get("type") == "text":
                    first_text = b.get("text", "")[:800]
                    break
        notify_user(
            f"[aside job {job['status']}] {job['title']}",
            "An aside-mcp-gateway async job finished.\n\n"
            f"- status: {job['status']}\n- title: {job['title']}\n"
            f"- job_id: {job_id}\n- result: {first_text or job.get('error', '')}")


GATEWAY_TOOLS = [
    {"name": "aside_job_start",
     "description": "Start a long browser task as an async job on a dedicated "
                    "Aside session. Returns immediately with a job_id. "
                    "Optional notify=true sends a Telegram notification on completion.",
     "inputSchema": {"type": "object",
                     "properties": {"title": {"type": "string"},
                                    "code": {"type": "string"},
                                    "notify": {"type": "boolean"}},
                     "required": ["title", "code"]}},
    {"name": "aside_job_status",
     "description": "Check an async job's status.",
     "inputSchema": {"type": "object",
                     "properties": {"job_id": {"type": "string"}},
                     "required": ["job_id"]}},
    {"name": "aside_job_result",
     "description": "Fetch an async job's result content blocks.",
     "inputSchema": {"type": "object",
                     "properties": {"job_id": {"type": "string"}},
                     "required": ["job_id"]}},
    {"name": "aside_preview_latest",
     "description": "Return the most recent screenshot any gateway session "
                    "produced (image block + age/client metadata).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "browser_list_tabs",
     "description": "Read-only: list currently open Aside browser tabs.",
     "inputSchema": {"type": "object", "properties": {}}},
]
GATEWAY_TOOL_NAMES = {t["name"] for t in GATEWAY_TOOLS}


def reap_idle_sessions():
    while True:
        time.sleep(60)
        now = time.time()
        with SESSIONS_LOCK:
            stale = [sid for sid, s in SESSIONS.items()
                     if now - s["child"].last_used > IDLE_TIMEOUT_SEC]
            victims = []
            for sid in stale:
                sess = SESSIONS.pop(sid)
                if sess.get("name"):
                    NAMED.pop((sess["client"], sess["name"]), None)
                victims.append(sess)
        for s in victims:
            audit({"event": "session_reaped", "client": s["client"]})
            s["child"].terminate()


# ---------------------------------------------------------------- http layer

def rpc_error(rid, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": err}


def text_result(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, status, obj, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, status, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _auth_client(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        token = auth[len("Bearer "):].strip()
        info = load_clients().get(token)
        if info is None:
            return None
        return info

    # -- routes
    def do_GET(self):
        if self.path == "/healthz":
            with SESSIONS_LOCK:
                n = len(SESSIONS)
            with JOBS_LOCK:
                nj = len(JOBS)
            self._send_json(200, {
                "ok": True, "service": "aside-mcp-gateway", "phase": 3,
                "sessions": n, "jobs": nj,
                "uptime_sec": int(time.time() - STARTED_AT),
                "aside_bin_ok": os.path.exists(ASIDE_BIN),
            })
        elif self.path == "/preview.jpg":
            client = self._auth_client()
            if not client:
                self._send_json(401, rpc_error(None, -32001, "unauthorized"))
                return
            import base64 as b64
            with PREVIEW_LOCK:
                latest = PREVIEW["latest"]
            if not latest:
                self._send_json(404, rpc_error(None, -32000, "no preview yet"))
                return
            raw = b64.b64decode(latest["data"])
            self.send_response(200)
            self.send_header("Content-Type", latest["mimeType"])
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif self.path == "/approvals":
            client = self._auth_client()
            if not client:
                self._send_json(401, rpc_error(None, -32001, "unauthorized"))
                return
            with APPROVALS_LOCK:
                pending = [{"id": r["id"], "client": r["client"],
                            "title": r["title"], "status": r["status"],
                            "expires_in": int(r["expires"] - time.time())}
                           for r in APPROVALS.values()
                           if r["status"] in ("pending", "approved")]
            self._send_json(200, {"pending": pending})
        elif self.path == "/mcp":
            self._send_json(405, rpc_error(None, -32000,
                                           "GET /mcp (SSE) not supported"))
        else:
            self._send_json(404, rpc_error(None, -32000, "not found"))

    def do_DELETE(self):
        client = self._auth_client()
        if not client:
            self._send_json(401, rpc_error(None, -32001, "unauthorized"))
            return
        sid = self.headers.get("Mcp-Session-Id")
        with SESSIONS_LOCK:
            sess = SESSIONS.pop(sid, None)
            if sess and sess.get("name"):
                NAMED.pop((sess["client"], sess["name"]), None)
        if sess:
            sess["child"].terminate()
            audit({"event": "session_closed", "client": client["name"]})
        self._send_json(200, {"ok": True})

    def do_POST(self):
        if self.path == "/approve":
            client = self._auth_client()
            if not client:
                self._send_json(401, rpc_error(None, -32001, "unauthorized"))
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                self._send_json(400, {"ok": False, "error": "bad json"})
                return
            with APPROVALS_LOCK:
                rec = APPROVALS.get(body.get("approval_id", ""))
                if client.get("role") == "approver" and rec \
                        and rec["client"] != client["name"] \
                        and rec["code"] == str(body.get("code", "")).strip() \
                        and time.time() <= rec["expires"] \
                        and rec["status"] == "pending":
                    rec["status"] = "approved"
                    ok = True
                else:
                    ok = False
            audit({"event": "approval_decision", "client": client["name"],
                   "approval_id": body.get("approval_id"), "ok": ok})
            self._send_json(200 if ok else 403,
                            {"ok": ok, "status": "approved" if ok else "rejected"})
            return

        if self.path != "/mcp":
            self._send_json(404, rpc_error(None, -32000, "not found"))
            return
        client = self._auth_client()
        if not client:
            self._send_json(401, rpc_error(None, -32001, "unauthorized"))
            return
        if client.get("role") == "approver":
            self._send_json(403, rpc_error(None, -32001, "approver token cannot call /mcp"))
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._send_json(400, rpc_error(None, -32700, "parse error"))
            return
        if isinstance(payload, list):
            responses, sid_out, extra = [], None, {}
            for item in payload:
                resp, sid, hdrs = self._handle_one(client, item)
                if sid:
                    sid_out = sid
                extra.update(hdrs or {})
                if resp is not None:
                    responses.append(resp)
            headers = dict(extra)
            if sid_out:
                headers["Mcp-Session-Id"] = sid_out
            self._send_json(200, responses, headers or None)
            return
        resp, sid, hdrs = self._handle_one(client, payload)
        headers = dict(hdrs or {})
        if sid:
            headers["Mcp-Session-Id"] = sid
        if resp is None:
            self._send_empty(202, headers or None)
            return
        self._send_json(200, resp, headers or None)

    # -- core dispatch
    def _handle_one(self, client, obj):
        """Returns (response_or_None, new_session_id_or_None, extra_headers)."""
        if not isinstance(obj, dict) or "method" not in obj:
            return rpc_error(obj.get("id") if isinstance(obj, dict) else None,
                             -32600, "invalid request"), None, None
        method = obj.get("method")
        rid = obj.get("id")
        sid = self.headers.get("Mcp-Session-Id")
        cname = client["name"]

        if method == "initialize":
            return self._initialize(client, obj)

        with SESSIONS_LOCK:
            sess = SESSIONS.get(sid) if sid else None
        if sess is None:
            return rpc_error(rid, -32000,
                             "unknown or missing Mcp-Session-Id (initialize first)"), None, None
        child = sess["child"]

        if method == "tools/list":
            try:
                resp = child.request(obj)
            except Exception as e:
                return rpc_error(rid, -32003, str(e)), None, None
            result = (resp or {}).get("result")
            if isinstance(result, dict) and isinstance(result.get("tools"), list):
                names = {t.get("name") for t in result["tools"]}
                result["tools"] = result["tools"] + \
                    [t for t in GATEWAY_TOOLS if t["name"] not in names]
            return resp, None, None

        if method == "tools/call":
            params = obj.get("params") or {}
            tname = params.get("name")
            args = params.get("arguments") or {}
            if tname in GATEWAY_TOOL_NAMES:
                return {"jsonrpc": "2.0", "id": rid,
                        "result": self._gateway_tool(client, child, tname, args)}, None, None
            if tname == "repl":
                gate_resp = self._gate_check(client, rid, args)
                if gate_resp is not None:
                    return gate_resp, None, None
            audit({"client": cname, "method": method, "tool": tname,
                   "title": args.get("title"),
                   "code_len": len(args.get("code") or "")})
            fwd = obj
            if tname == "repl" and any(k.startswith("_") for k in args):
                clean_args = {k: v for k, v in args.items()
                              if not k.startswith("_")}
                fwd = dict(obj)
                fwd["params"] = dict(params)
                fwd["params"]["arguments"] = clean_args
            try:
                resp = child.request(fwd)
            except ChildDead:
                with SESSIONS_LOCK:
                    dead = SESSIONS.pop(sid, None)
                    if dead and dead.get("name"):
                        NAMED.pop((dead["client"], dead["name"]), None)
                return rpc_error(rid, -32003,
                                 "aside mcp process died; re-initialize"), None, None
            except TimeoutError as e:
                return rpc_error(rid, -32004, str(e)), None, None
            cache_preview(cname, (resp or {}).get("result"))
            return resp, None, None

        try:
            resp = child.request(obj)
        except Exception as e:
            return rpc_error(rid, -32003, str(e)), None, None
        return resp, None, None

    def _initialize(self, client, obj):
        cname = client["name"]
        name = self.headers.get("X-Aside-Session")
        rid = obj.get("id")
        if name:
            with SESSIONS_LOCK:
                old_sid = NAMED.get((cname, name))
                sess = SESSIONS.get(old_sid) if old_sid else None
            if sess and sess["child"].alive:
                audit({"event": "session_resumed", "client": cname, "name": name})
                return sess["child"].init_resp, old_sid, \
                    {"X-Aside-Session-Resumed": "true"}
        child = ChildSession(cname)
        try:
            resp = child.request(obj)
        except Exception as e:
            child.terminate()
            return rpc_error(rid, -32002, f"aside mcp init failed: {e}"), None, None
        child.init_resp = resp
        new_sid = uuid.uuid4().hex
        with SESSIONS_LOCK:
            SESSIONS[new_sid] = {"child": child, "name": name, "client": cname}
            if name:
                NAMED[(cname, name)] = new_sid
        audit({"event": "session_created", "client": cname, "name": name,
               "method": "initialize"})
        return resp, new_sid, {"X-Aside-Session-Resumed": "false"} if name else None

    # -- safety gate
    def _gate_check(self, client, rid, args):
        """Returns an error response if the call must not proceed, else None."""
        code = args.get("code") or ""
        title = args.get("title")
        cname = client["name"]
        hit = denylist_hit(code)
        if hit:
            audit({"event": "gate_blocked_denylist", "client": cname,
                   "title": title, "url": hit})
            return rpc_error(rid, -32011,
                             f"blocked by denylist: {hit}",
                             {"denylist": hit})
        level, reasons = classify_code(code)
        mode = client.get("gate", GATE_DEFAULT_MODE)
        need = (level == "destructive" and mode in ("approve-destructive",
                                                     "approve-write")) or \
               (level == "write" and mode == "approve-write")
        audit({"event": "gate_classified", "client": cname, "title": title,
               "level": level, "reasons": reasons, "mode": mode,
               "approval_required": need})
        if mode == "off" or not need:
            return None
        approval_arg = args.get("_approval")
        approval_id_arg = args.get("_approval_id")
        code_sha = hashlib.sha256(code.encode()).hexdigest()
        with APPROVALS_LOCK:
            rec = APPROVALS.get(approval_id_arg) if approval_id_arg else None
            # retry carrying the code itself counts as approval
            if rec is not None and approval_arg is not None \
                    and str(approval_arg).strip() == rec["code"] \
                    and rec["status"] == "pending" \
                    and time.time() <= rec["expires"] \
                    and rec["client"] == cname \
                    and rec["code_sha"] == code_sha:
                rec["status"] = "approved"
            if rec is not None and approval_state(rec, cname, code) == "ok":
                audit({"event": "gate_approved_execute", "client": cname,
                       "approval_id": rec["id"], "title": title})
                return None
            # dedupe: reuse an existing pending approval for the same code
            if rec is None or rec["status"] not in ("pending", "approved"):
                for r in APPROVALS.values():
                    if r["client"] == cname and r["code_sha"] == code_sha \
                            and r["status"] == "pending" \
                            and time.time() <= r["expires"]:
                        rec = r
                        break
                else:
                    rec = None
        if rec is not None:
            return rpc_error(rid, -32010,
                             "approval required for destructive action",
                             {"approval_required": True,
                              "approval_id": rec["id"], "level": level,
                              "reasons": reasons,
                              "expires_in_sec": int(rec["expires"] - time.time()),
                              "how": "Ask the user for the approval code (sent "
                                     "via Telegram), then retry the same call "
                                     "with _approval_id and _approval set, or "
                                     "POST /approve."})
        # not approved -> register a new pending approval + notify
        rec = new_approval(cname, code, title)
        delivered = tg_send(
            f"🔐 aside gateway approval request\n"
            f"- client: {cname} · {title or 'destructive action'}\n"
            f"- reason: {', '.join(reasons)}\n"
            f"- code preview: {rec['preview']}\n"
            f"- Approval code: {rec['code']} (valid {APPROVAL_TTL_SEC // 60} min)\n\n"
            f"To allow it, give this code to the requesting agent or run scripts/approve.py {rec['code']}. Ignore it and nothing runs.")
        data = {"approval_required": True, "approval_id": rec["id"],
                "level": level, "reasons": reasons,
                "expires_in_sec": APPROVAL_TTL_SEC,
                "how": "The approval code was sent to the user's Telegram only. "
                       "Wait for the user, then retry the same call with "
                       "_approval_id (and _approval if the user gave you the code)."}
        if not delivered:
            # fail closed: never hand the code to the requester
            rec["status"] = "expired"
            data["warning"] = "telegram notify failed; approval impossible, ask the user"
            audit({"event": "gate_notify_failed_closed", "client": cname,
                   "approval_id": rec["id"]})
        return rpc_error(rid, -32010,
                         "approval required for destructive action", data)

    # -- gateway-native tools
    def _gateway_tool(self, client, child, tname, args):
        cname = client["name"]
        audit({"client": cname, "method": "tools/call", "tool": tname})
        if tname == "browser_list_tabs":
            try:
                resp = child.call_repl(
                    "List tabs (gateway)",
                    "globalThis.__gwTabs = await listBrowserTabs(); "
                    "console.log(JSON.stringify(globalThis.__gwTabs));", rid=55)
                return (resp or {}).get("result") or text_result("no result", True)
            except Exception as e:
                return text_result(f"error: {e}", True)
        if tname == "aside_preview_latest":
            with PREVIEW_LOCK:
                latest = PREVIEW["latest"]
            if not latest:
                return text_result("no screenshot captured yet")
            age = int(time.time() - latest["ts"])
            return {"content": [
                {"type": "text",
                 "text": f"latest preview: {age}s ago, client={latest['client']}"},
                {"type": "image", "data": latest["data"],
                 "mimeType": latest["mimeType"]}], "isError": False}
        if tname == "aside_job_start":
            code = args.get("code") or ""
            title = args.get("title") or "job"
            hit = denylist_hit(code)
            if hit:
                return text_result(f"blocked by denylist: {hit}", True)
            level, reasons = classify_code(code)
            if level == "destructive":
                return text_result(
                    "destructive code cannot run as a job without approval; "
                    f"reasons: {', '.join(reasons)}", True)
            job_id = uuid.uuid4().hex[:10]
            job = {"id": job_id, "client": cname, "title": title, "code": code,
                   "status": "running", "created": time.time(),
                   "finished": None, "result": None, "error": None,
                   "notify": args.get("notify", JOB_NOTIFY_DEFAULT)}
            with JOBS_LOCK:
                JOBS[job_id] = job
            threading.Thread(target=run_job, args=(job_id,), daemon=True).start()
            audit({"event": "job_started", "job": job_id, "client": cname,
                   "title": title, "level": level})
            return text_result(json.dumps(
                {"job_id": job_id, "status": "running", "title": title}))
        if tname == "aside_job_status":
            with JOBS_LOCK:
                job = JOBS.get(args.get("job_id", ""))
            if not job:
                return text_result("unknown job_id", True)
            return text_result(json.dumps({
                "job_id": job["id"], "status": job["status"],
                "title": job["title"],
                "elapsed_sec": int((job["finished"] or time.time()) - job["created"]),
                "error": job["error"]}))
        if tname == "aside_job_result":
            with JOBS_LOCK:
                job = JOBS.get(args.get("job_id", ""))
            if not job:
                return text_result("unknown job_id", True)
            if job["status"] == "running":
                return text_result("still running")
            if job["status"] == "error":
                return text_result(f"job error: {job['error']}", True)
            return job["result"] or text_result("no result")
        return text_result(f"unknown gateway tool: {tname}", True)


def main():
    if not os.path.exists(ASIDE_BIN):
        print(f"aside binary not found: {ASIDE_BIN}", file=sys.stderr)
        sys.exit(1)
    threading.Thread(target=reap_idle_sessions, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"aside-mcp-gateway (phase 3) listening on {HOST}:{PORT}", flush=True)
    audit({"event": "gateway_started", "host": HOST, "port": PORT, "phase": 3})
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
