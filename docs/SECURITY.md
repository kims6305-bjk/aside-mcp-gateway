# Security model

[README](../README.md) · [README (English)](../README.en.md)

`aside-mcp-gateway` hands an AI agent a **real, logged-in browser**. The design
goal is that an agent can do useful work without being able to do anything
irreversible on its own.

## Threats considered

| Threat | Mitigation |
|---|---|
| Someone on the internet reaches the gateway | Bind to a private-network address only (Tailscale/VPN/LAN); never `0.0.0.0` on a public host |
| A stolen/leaked client token | Per-client Bearer tokens in `tokens.json` (mode 600); revoke one entry without touching the others |
| An agent pays, sends, posts, submits or deletes by itself | Every `repl` call is classified read / write / destructive; destructive calls need a one-time human approval |
| **The requesting agent approves itself** | See the two principles below |
| An agent reads gateway secrets through the REPL's `fs` | Code mentioning secret paths (`.ssh`, `.env`, `.aws`, `.gnupg`, `tokens.json`, the gateway dir, Keychains) is classified destructive |
| Audit log leaks page content or credentials | `logs/audit.jsonl` records title, length and grade only — never code bodies or approval codes |

## Two approval principles

1. **The approval code never travels through a channel the requesting agent
   can read.** Not in the RPC response, not in an issue tracker, not in an
   outbox file, not in the audit log. It goes to the operator's Telegram DM
   only. If that delivery fails, the approval is expired immediately
   (fail-closed) — the code is never handed out as a fallback.
2. **Approval is granted by a party other than the requester.** `POST /approve`
   accepts only tokens with `"role": "approver"` whose name differs from the
   requesting client, and approver tokens are refused on `/mcp` (403), so the
   approver cannot also drive the browser.

The operator can also pass the code to the agent directly; the agent then
retries with `_approval_id` + `_approval`. That is safe because only the
operator could have read the code.

### Why these two rules exist

An earlier version posted the code into a GitHub issue that the bridge agent
forwarded to the operator. The requesting agent used the same GitHub account,
so it could read the issue and call `/approve` itself — the audit log showed
destructive test calls approved eight seconds after the request, before any
human saw them. A second path handed the code back in the RPC response when
issue creation failed. Both are closed, and
[`tests/test_gate_selfapprove.py`](../tests/test_gate_selfapprove.py) fails on
the old behaviour (code in response) and passes on the current one.

## Known limits

- **Pattern classification is heuristic.** Code that builds a path from string
  pieces can evade the secret-path rule, and new destructive APIs may be
  missed. Unclassified code defaults to *write*, not *read*. The real fix for
  secret isolation is running Aside under a **separate OS user** that cannot
  read the gateway's files.
- **Write-level actions are not approved by default** (`approve-destructive`).
  Use `"gate": "approve-write"` for clients you trust less.
- **Telegram is a single delivery channel.** If you cannot use Telegram,
  replace `tg_send()` with another operator-only channel; keep principle 1.
