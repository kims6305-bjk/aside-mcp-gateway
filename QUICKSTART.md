# QUICKSTART — set up your own aside-mcp-gateway

This guide takes you from zero to a running gateway on **your** machine.
Korean speakers can also read [README.md](README.md); this file is the
step-by-step for third-party operators.

## 0. What you need

| Requirement | Why | How to get it |
|---|---|---|
| A machine that stays on (Mac mini, home server, …) | The gateway must be reachable whenever you want to use it | — |
| **Aside AI browser + CLI, logged in** | The gateway wraps the official `aside mcp` command | Install Aside, then the CLI (`~/.local/bin/aside`), then sign in inside the Aside app / CLI |
| Python 3.9+ (stdlib only) | The gateway has zero dependencies | `python3 --version` |
| A private network between your clients and the server | The gateway controls a **real logged-in browser** — never expose it to the internet | [Tailscale](https://tailscale.com) (recommended), or any VPN / LAN |
| A Telegram bot (for approvals) | Destructive actions need a one-time code delivered to **you** | [@BotFather](https://t.me/BotFather) → `/newbot` → save the token; then message your bot once and get your chat id via `https://api.telegram.org/bot<TOKEN>/getUpdates` |

> Without a working Telegram setup the safety gate **fails closed**: destructive
> calls are blocked and can never be approved. Read-only browsing still works.

## 1. Clone and configure

```sh
git clone https://github.com/kims6305-bjk/aside-mcp-gateway
cd aside-mcp-gateway
cp config.example.json config.json
```

Edit `config.json`:

```jsonc
{
  "aside_bin": "/Users/you/.local/bin/aside",   // path to the Aside CLI
  "host": "100.x.y.z",        // your Tailscale IP — NOT 0.0.0.0
  "port": 11441,
  "tokens_file": "./tokens.json",
  "log_dir": "./logs",
  "idle_timeout_sec": 2100,   // child process reaped after 30 min idle
  "request_timeout_sec": 180,
  "gate": {
    "default_mode": "approve-destructive",  // or "approve-write" / "off"
    "approval_ttl_sec": 600,
    "denylist_url_patterns": []
  },
  "notify": { "job_notify_default": true },
  "telegram": { "env_file": "~/.config/aside-gateway-telegram.env" }
}
```

Create the Telegram env file (keep it `chmod 600`, never commit it):

```sh
cat > ~/.config/aside-gateway-telegram.env <<'EOF'
TELEGRAM_BOT_TOKEN=123456:ABC-your-bot-token
TELEGRAM_HOME_CHANNEL=987654321
EOF
chmod 600 ~/.config/aside-gateway-telegram.env
```

(Alternatively put `"bot_token"` / `"chat_id"` directly in the `telegram`
section of `config.json` — same secrecy rules apply.)

## 2. Create tokens

`tokens.json` (also `chmod 600`, never commit):

```json
{
  "tokens": {
    "<random-string-1>": "my-laptop",
    "<random-string-2>": { "name": "my-bot", "gate": "approve-write" },
    "<random-string-3>": { "name": "approver", "role": "approver" }
  }
}
```

- Generate each token with e.g. `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
- Plain string value = a normal client (uses the default gate mode).
- `gate` can be `approve-destructive` (default), `approve-write`, or `off`.
- You need **one approver token** (`role: approver`): it is the only token
  allowed to call `POST /approve`, and it **cannot** call `/mcp` (403 by design).
  Keep its value somewhere safe — it never goes into the repo.

## 3. Run it

```sh
python3 gateway.py
# → aside-mcp-gateway (phase 3) listening on 100.x.y.z:11441
```

Smoke test:

```sh
curl http://100.x.y.z:11441/healthz
# {"ok": true, "service": "aside-mcp-gateway", "phase": 3, ...}
```

Keep it resident:

- **macOS**: copy `launchd/com.example.aside-mcp-gateway.plist` to
  `~/Library/LaunchAgents/`, fix the 3 paths inside, then
  `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.aside-mcp-gateway.plist`
- **Linux**: write a small systemd user unit that runs `python3 /path/to/gateway.py`
  with `WorkingDirectory` set to the repo dir.

## 4. Connect a client

**Claude Code:**

```sh
claude mcp add --scope user --transport http aside \
  http://100.x.y.z:11441/mcp \
  --header "Authorization: Bearer <random-string-1>"
```

**Claude Desktop:** add to `claude_desktop_config.json`:

```json
{ "mcpServers": { "aside": {
  "url": "http://100.x.y.z:11441/mcp",
  "headers": { "Authorization": "Bearer <random-string-1>" }
}}}
```

**ChatGPT (developer mode):** use the official OpenAI Secure MCP Tunnel
(`tunnel-client`) pointing at your gateway URL, as documented in the
`tunnel-client` onboarding. Your MCP client then talks to the tunnel URL.

Give your agent the [SKILL.md](SKILL.md) (`aside-browser`) so it knows the
tool surface, the named-session header, and the approval flow.

## 5. How approval works (operator view)

1. Agent runs a destructive snippet (file delete, payment, form submit, `eval`, …)
   → gateway blocks it, returns `approval_required` + `approval_id`.
2. Your Telegram gets: `🔐 aside gateway approval request` + 6-digit code.
3. Either tell the agent the code (it retries with `_approval_id` + `_approval`),
   or approve yourself with the approver token:
   `ASIDE_GW_URL=http://100.x.y.z:11441 ASIDE_APPROVER_TOKEN=<approver> python3 scripts/approve.py 123456`
   — then the agent retries with just `_approval_id`.
4. The call runs **once**. Ignore it and nothing happens; the code expires in 10 min.

The requesting agent can never see the code: it is not in the RPC response,
not in any file, not in the audit log — Telegram DM only.

## 6. Run the tests

```sh
python3 tests/test_gate_selfapprove.py   # 13 checks: the approval-gate contract
python3 tests/test_phase23.py http://127.0.0.1:11441 <token> ./outbox
python3 tests/test_http.py  http://127.0.0.1:11441 <token>
```

The first two need a gateway running against `tests/mock_mcp.py` (see the test
headers); the self-approval test is fully in-process.

## 7. Security checklist before you go live

- [ ] `host` is a private/VPN IP, not `0.0.0.0`.
- [ ] `tokens.json` and the Telegram env file are `chmod 600` and in `.gitignore`.
- [ ] The repo you cloned has no `config.json` / `tokens.json` committed (it shouldn't).
- [ ] Telegram approval DM actually arrives (send a test destructive call).
- [ ] Consider running Aside under a **separate OS user** — the secret-path
      pattern block is heuristic; OS-level separation is the real fix.
- [ ] Backups: `tokens.json` holds your credentials to the gateway — back it up
      somewhere safe, offline.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `401` on `/mcp` | Wrong/missing `Authorization: Bearer` header |
| Destructive calls always fail with "telegram notify failed" | `telegram` section misconfigured — check the env file path and token |
| `aside mcp` child dies immediately | `aside_bin` path wrong, or the Aside CLI is not logged in on this machine |
| Session lost its JS variables | Child reaped after 30 min idle, or gateway restarted — use `X-Aside-Session` named sessions for continuity |
| `fs.writeFile` → ENOENT under session dir | The `tmp/` subdir may not exist until the first `display()` — `mkdir` it first |
