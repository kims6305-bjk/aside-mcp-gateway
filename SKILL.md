---
name: aside-browser
description: Use when an agent must drive a real, logged-in Aside browser through an aside-mcp-gateway (open/read pages, click/type via Playwright-style JS, screenshots, long async jobs) from any MCP client or the bundled CLI. Destructive actions need a human approval code.
---

# aside-browser — drive a shared Aside browser through aside-mcp-gateway

The operator runs `aside-mcp-gateway` on an always-on machine. It exposes the
official `aside mcp` server over HTTP on a private network. Anything you do
through it happens in the operator's **real, logged-in browser** — treat it with
the same care as using their browser by hand.

## Endpoint and auth

- URL: `http://<gateway-host>:11441/mcp` — ask the operator for the host. It is
  bound to a private network (Tailscale/VPN/LAN), never the public internet.
- Health: `GET http://<gateway-host>:11441/healthz`
- Auth: `Authorization: Bearer <token>` — one token per client. Ask the
  operator which token is yours; never print, share or commit it.
- If your runtime can only reach the private network through an HTTP proxy,
  set `ASIDE_GW_PROXY` for the bundled CLI.

## Tool surface

The core server exposes one tool, `repl {title: string, code: string}`. It runs
Playwright-style JavaScript in a persistent sandbox attached to the browser.
The gateway adds:

- `aside_job_start {title, code, notify?}` — long task as an async job on a
  dedicated session; returns `job_id` at once. `notify=true` (default) pings
  the operator on completion.
- `aside_job_status {job_id}` / `aside_job_result {job_id}` — poll the job.
- `aside_preview_latest {}` — newest screenshot from any session (image + age).
- `browser_list_tabs {}` — read-only list of open tabs.

`repl` facts:

- Return data with `console.log(...)`; `return` values are discarded.
- Scope persists across calls **within one MCP session**; do not redeclare the
  same `const`/`let` name — use fresh names or `globalThis`.
- One session = one server process, reaped after ~30 min idle (variables are
  lost, tabs are not).
- Tabs are **shared across all sessions**. Check `listBrowserTabs()` first and
  never close tabs you did not open.
- Per-call timeout ~120 s. Compose long flows from several small calls.

Globals: `listBrowserTabs()`, `attachBrowserTab(targetId)`,
`attachActiveBrowserTab()`, `openTab(url)`, `closeTab(page)`,
`snapshot(page)` → `{tree, diff}` (primary way to read a page),
`page.screenshot()` + `display(bytes)` (returns an image block),
`annotatedScreenshot(page)`, Playwright `page.locator(...)`, `fs` (session
workdir), `fetch` (carries the user's cookies), `sleep(ms)`.

```js
const tabs1 = await listBrowserTabs();           // 1. reuse before opening
console.log(JSON.stringify(tabs1));
const s1 = await snapshot(page);                 // 2. read before acting
console.log(s1.tree);
await page.locator('e3').click();                // 3. act, then read the diff
const s2 = await snapshot(page);
console.log(s2.diff);
```

## Ways to call it

1. **MCP-native client** — e.g. Claude Code:
   `claude mcp add --scope user --transport http aside http://<gateway-host>:11441/mcp --header "Authorization: Bearer <token>"`
2. **Bundled CLI** (any agent with a shell):
   ```sh
   export ASIDE_GW_URL=http://<gateway-host>:11441
   export ASIDE_GW_TOKEN=<token>
   python3 scripts/gateway_client.py --title "List tabs" \
     --code "console.log(JSON.stringify(await listBrowserTabs()))"
   ```
   Each CLI run is a fresh session; pass several `--code` steps for stateful flows.

Named sessions: send `X-Aside-Session: <name>` on `initialize` to resume the
same process and JS scope (`X-Aside-Session-Resumed: true` confirms it).

## Safety rules and the approval gate

- Real logins: `fetch` carries cookies, `fs` can write. Never exfiltrate page
  content, cookies or files.
- Every `repl` call is classified read / write / destructive. Destructive code
  (file deletion, payment/checkout, send/publish/submit, `eval`, external
  POST/PUT/DELETE, reads of secret paths such as `.ssh`/`.env`/`tokens.json`)
  is **blocked**: you get error `-32010` with an `approval_id`, and the
  operator alone receives a 6-digit code on Telegram. You never see the code.
- Then tell the operator exactly what is blocked and wait. Retry the same call
  with `_approval_id` (plus `_approval` if the operator gives you the code).
  Do not try to obtain the code any other way — that is the attack the gate
  exists to stop.
- Never submit payments, send messages/posts, delete data or press final
  confirm/purchase/send controls without the operator's explicit go-ahead in
  the current conversation. Prepare everything up to that point, then ask.
- One task at a time; no bulk tab opening or tight request loops.

## Troubleshooting

- `401` → missing/wrong token. `403` on `/mcp` → you are using an approver token.
- `unknown or missing Mcp-Session-Id` → session expired or gateway restarted;
  initialize again.
- `warning: telegram notify failed` → the operator's Telegram setup is broken;
  approval is impossible until they fix it (fail-closed by design).
- `aside mcp process died` → the Aside app is closed or logged out on the
  gateway machine; only the operator can fix it. Report, do not loop.
