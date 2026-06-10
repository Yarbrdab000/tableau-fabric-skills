# Workflow: Consume the deployed endpoint

Standing up the landing zone is only half the job — a person still has to *consume* the endpoint
from a client. This guide is the **consumption decision point**: after the deploy is verified, ask
the user **"Where do you want to consume this?"** and follow the matching section below. Tools are
discovered automatically over MCP; you never declare each tool by hand.

> **Route, don't guess.** There is no single right client. Ask which one the user wants, jump to
> that section, and only configure that client.

## Shared facts (every client inherits these)

| Fact | Value |
|---|---|
| **Endpoint** | the `mcpEndpoint` deploy output — an HTTPS URL ending in `/mcp`, e.g. `https://<app>.<region>.azurecontainerapps.io/mcp`. Local stack: `http://localhost:9000/mcp` (see [local-dev.md](local-dev.md)). |
| **Transport** | **Streamable HTTP**. |
| **Auth** | request header **`x-api-key: <sidecarApiKey>`**. If a client only offers a bearer field, use `Authorization: Bearer <sidecarApiKey>` — the sidecar accepts either. |
| **Cold start** | the Container App scales to zero; the first call after idle takes a few seconds. Retry. |
| **Curated tools** | `list-datasources`, `get-datasource-metadata`, `query-datasource`, `search-content` (see the tool-curation note in [copilot-studio-wiring.md](copilot-studio-wiring.md)). |
| **Identity** | a key-only client acts as the deployment's **`service_account`** Tableau identity. Per-user RLS (`passthrough`) needs Easy Auth **plus** an identity-carrying caller — see [Identity scope](#identity-scope-what-each-client-sees). |

> ⚠️ **Client config syntax changes fast.** The configs below are current at time of writing;
> where a step is version-sensitive it's flagged **"confirm for your version"** with a link to the
> vendor's MCP docs. Prefer the client's guided "add server" command over hand-editing when one exists.

## Where do you want to consume this?

Ask the user, then go to the matching section:

- **[Microsoft 365 Copilot](#1-microsoft-365-copilot-do-first)** — chat with Tableau inside M365 Copilot (Word/Teams/Outlook/web).
- **[Copilot Studio agent (Teams / standalone)](#2-copilot-studio-agent-teams--standalone)** — a custom agent surfaced in Teams.
- **[GitHub Copilot CLI (this client)](#3-github-copilot-cli-this-client)**
- **[VS Code Copilot (agent mode)](#4-vs-code-copilot-agent-mode)**
- **[Claude Code](#5-claude-code)**
- **[Claude Desktop](#6-claude-desktop)**
- **[Cursor](#7-cursor)**
- **[Generic MCP client / curl](#8-generic-mcp-client--curl)**
- *(ChatGPT — deferred; see the [note](#chatgpt-deferred).)*

---

## 1. Microsoft 365 Copilot (DO FIRST)

Surface Tableau as an **agent inside Microsoft 365 Copilot**. M365 Copilot doesn't consume a raw MCP
endpoint directly — you wire the endpoint into a **Copilot Studio agent**, then publish that agent to
the Microsoft 365 Copilot channel.

**Prerequisites**

- The endpoint deployed and verified. For **per-user row-level security**, deploy with
  `identityMode=passthrough` + Easy Auth so each signed-in M365 user queries as themselves; for a
  shared demo, `service_account` works but every user sees the same data.
- A **Copilot Studio** environment with **generative orchestration ON** (MCP tools are ignored under
  classic orchestration).
- Each end user needs a **Microsoft 365 Copilot license**.
- Rights to **admin-approve** the agent for org-wide use (or an admin who can).

**Steps**

1. **Wire the MCP endpoint into a Copilot Studio agent** — follow Option A (custom connector) or
   Option B (built-in MCP tool) in [copilot-studio-wiring.md](copilot-studio-wiring.md). Confirm the
   `x-api-key` connection works in the agent's Test pane first.
2. **Publish the agent at least once** (Copilot Studio → **Publish**).
3. **Connect the "Teams and Microsoft 365 Copilot" channel:** open its configuration panel, ensure
   **"Make agent available in Microsoft 365 Copilot"** is selected, then **Add channel**.
4. **Install for yourself (self-serve — no admin needed):** select **See agent in Teams** (this
   installs to both Teams and M365 Copilot). In M365 Copilot, type **`@`**, pick your agent, and ask
   a question. This is enough for your own live test.
5. **Make it available org-wide (requires a Teams/M365 admin — NOT self-serve):** submit the agent
   for **admin approval** so it appears in the **Built for your org** section of the Teams app store /
   **Built by your org** in the Microsoft 365 Agent Store. A **tenant admin** approves it in the
   **Teams admin center / Microsoft 365 admin center** (Integrated Apps / Manage apps) — a non-admin
   maker cannot complete this step alone, so involve IT before expecting org-wide visibility.
6. **For per-user RLS:** turn on **end-user authentication** on the agent and keep the deployment in
   `passthrough` + Easy Auth so the signed-in Entra identity flows through to Tableau.

**Test** — in M365 Copilot: `@<your agent> What were the top 3 regions by total sales?` → the agent
should call `query-datasource` and answer from live Tableau data.

**What you're testing — the agent layer, not raw Copilot chat**

The endpoint is consumed as **MCP → connector/action → Copilot Studio agent → M365 Copilot
(orchestrator)**. Raw M365 Copilot chat will **not** auto-call the MCP endpoint on its own — that's
expected. The end-state test is the **agent**: prompt it from the agent's **Test panel** (or once it's
surfaced in Teams / M365 Copilot) and watch agent → MCP → data → formatted answer.

**Validate in this order (cheap → rich):**

1. **MCP standalone** — hit `/mcp` directly with Copilot CLI, `verify_deployment.py`, or a `curl`
   initialize. Fastest signal, no M365 setup. (See [§8](#8-generic-mcp-client--curl).)
2. **Personal Copilot Studio agent + MCP tool → Test panel** — add the endpoint as a tool on a
   personal agent and prompt it in the Test panel. **No admin or publish needed** — this is the real
   M365-like end-state.
3. **Org-wide publish** — the admin-gated leg (step 5 above); only needed to share the agent beyond
   yourself.

**Gotchas**

- Generative orchestration **must** be ON, or the agent ignores the MCP tools.
- Self-install works immediately for the maker; **org-wide visibility requires admin approval**.
- Without Easy Auth/passthrough, every M365 user queries as the single `service_account` (shared
  view, no per-user RLS).
- Copilot Studio's channel/publish UI and labels shift frequently — **confirm for your version**
  against [Connect an agent to Teams and Microsoft 365 Copilot](https://learn.microsoft.com/en-us/microsoft-copilot-studio/publication-add-bot-to-microsoft-teams).

## 2. Copilot Studio agent (Teams / standalone)

If the target is a **Copilot Studio agent** (used in Teams or standalone) rather than M365 Copilot
itself, the wiring is the same first step as above — register the endpoint as a tool, then publish
to Teams. The full walkthrough (Option A custom connector / Option B built-in MCP tool, the Test
pane, and tool curation) lives in **[copilot-studio-wiring.md](copilot-studio-wiring.md)**.

## 3. GitHub Copilot CLI (this client)

**Interactive:** run `/mcp add`, then fill the form:

- **Server Name** `tableau`
- **Server Type** `HTTP` (Streamable HTTP)
- **URL** your MCP endpoint (ends in `/mcp`)
- **HTTP Headers** `{"x-api-key": "YOUR-SIDECAR-API-KEY"}`
- **Tools** `*`

Save with <kbd>Ctrl</kbd>+<kbd>S</kbd>; it's available immediately. Manage with `/mcp show`,
`/mcp show tableau`, `/mcp edit tableau`, `/mcp delete tableau`.

**Config file** — `~/.copilot/mcp-config.json` (Windows: `%USERPROFILE%\.copilot\mcp-config.json`):

```json
{
  "mcpServers": {
    "tableau": {
      "type": "http",
      "url": "https://<app>.<region>.azurecontainerapps.io/mcp",
      "headers": { "x-api-key": "YOUR-SIDECAR-API-KEY" },
      "tools": ["*"]
    }
  }
}
```

**Test** — ask: *"What Tableau datasources can you see?"* → the agent calls `list-datasources`.

## 4. VS Code Copilot (agent mode)

**Prerequisites** — VS Code with GitHub Copilot, Chat in **agent mode**. MCP support is current in
recent VS Code releases; **confirm for your version** against
[Add MCP servers in VS Code](https://code.visualstudio.com/docs/agent-customization/mcp-servers).

**Config** — create `.vscode/mcp.json` in the workspace (or run **MCP: Open User Configuration** for
a global one, or **MCP: Add Server** for the guided flow). Use an **input** so the key is prompted,
not hardcoded:

```json
{
  "inputs": [
    {
      "type": "promptString",
      "id": "tableau-key",
      "description": "Tableau MCP sidecar API key",
      "password": true
    }
  ],
  "servers": {
    "tableau": {
      "type": "http",
      "url": "https://<app>.<region>.azurecontainerapps.io/mcp",
      "headers": { "x-api-key": "${input:tableau-key}" }
    }
  }
}
```

Start/trust the server when prompted. Use the **Configure Tools** button in the Chat input to confirm
the `tableau` server's tools are listed.

**Test** — in agent-mode Chat: *"What fields are in the Superstore datasource?"* → `get-datasource-metadata`.

**Gotchas** — don't hardcode the key (use the `inputs` prompt or an env file); a workspace
`.vscode/mcp.json` is shareable, so keep the literal key out of it; the schema is version-sensitive.

## 5. Claude Code

**CLI:**

```bash
claude mcp add --transport http tableau \
  https://<app>.<region>.azurecontainerapps.io/mcp \
  --header "x-api-key: YOUR-SIDECAR-API-KEY"
```

Add `--scope user` to make it available in every project, or `--scope project` to write a shared
`.mcp.json` (if you do, do **not** hardcode the key — see [Secret discipline](#secret-discipline)).
Flag names move occasionally — **confirm for your version** with `claude mcp add --help`.

**Config file** — project `.mcp.json` or user `~/.claude.json`:

```json
{
  "mcpServers": {
    "tableau": {
      "type": "http",
      "url": "https://<app>.<region>.azurecontainerapps.io/mcp",
      "headers": { "x-api-key": "YOUR-SIDECAR-API-KEY" }
    }
  }
}
```

In JSON, `type` accepts `streamable-http` as an alias for `http`, so configs copied from server docs
work unchanged.

**Test** — `/mcp` lists the `tableau` server; ask an NL query.

## 6. Claude Desktop

Claude Desktop's native config is **stdio-only** — it can't take a remote HTTP MCP URL directly. Two
routes:

**(a) Bridge with `mcp-remote` (recommended).** Edit `claude_desktop_config.json`
(Windows: `%APPDATA%\Claude\claude_desktop_config.json`; macOS:
`~/Library/Application Support/Claude/claude_desktop_config.json`), then restart Claude Desktop:

```json
{
  "mcpServers": {
    "tableau": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote",
        "https://<app>.<region>.azurecontainerapps.io/mcp",
        "--transport", "http-only",
        "--header", "x-api-key:${TABLEAU_MCP_KEY}"
      ],
      "env": { "TABLEAU_MCP_KEY": "YOUR-SIDECAR-API-KEY" }
    }
  }
}
```

- Use **`--transport http-only`** — our endpoint is Streamable HTTP at `/mcp`; without it
  `mcp-remote` may probe for an SSE endpoint first.
- Write the header as **`x-api-key:${TABLEAU_MCP_KEY}`** (no space after the colon) with the value in
  `env`. Claude Desktop on Windows has a known bug where spaces inside `args` get mangled when it
  invokes `npx`; this pattern avoids it.
- Requires **Node 18+**. `mcp-remote` is an experimental community bridge — see its
  [README](https://github.com/geelen/mcp-remote).

**(b) Claude.ai Custom Connectors** — add the `/mcp` URL as a custom connector. This is **plan-gated**
(availability depends on your Claude tier); confirm in your workspace.

**Test** — after restart, the tools (hammer icon) should appear; ask an NL query. If it wedges, clear
`~/.mcp-auth` and restart.

## 7. Cursor

**Config file** — global `~/.cursor/mcp.json` or project `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "tableau": {
      "url": "https://<app>.<region>.azurecontainerapps.io/mcp",
      "headers": { "x-api-key": "${env:TABLEAU_MCP_API_KEY}" }
    }
  }
}
```

Cursor resolves `${env:NAME}` in `url` and `headers`, so set `TABLEAU_MCP_API_KEY` in your shell
profile rather than pasting the key into the file. After saving, enable the server in
**Settings → MCP**.

## 8. Generic MCP client / curl

Any Streamable-HTTP MCP client works: point it at the `/mcp` URL with header `x-api-key: <key>`.

**Quick checks** (no MCP client needed):

```bash
# Health (no auth):
curl -s https://<app>.<region>.azurecontainerapps.io/healthz          # -> {"status":"ok", ...}

# Auth is enforced (no key -> rejected):
curl -s -o /dev/null -w "%{http_code}\n" \
  -X POST https://<app>.<region>.azurecontainerapps.io/mcp            # -> 401
```

**Full handshake** — the bundled verifier does a real `initialize` + `tools/list` for you:

```bash
# key in the environment, never on the command line:
export SIDECAR_API_KEY=YOUR-SIDECAR-API-KEY
python scripts/verify_deployment.py --base-url https://<app>.<region>.azurecontainerapps.io
```

**Raw `initialize` over curl** (illustrative — protocol version is client-specific, **confirm for
your version**). Streamable HTTP requires the dual `Accept` header and returns an SSE stream:

```bash
curl -sS -X POST "https://<app>.<region>.azurecontainerapps.io/mcp" \
  -H "x-api-key: YOUR-SIDECAR-API-KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

### ChatGPT (deferred)

ChatGPT custom connectors — **deferred**: they need OAuth / Entra Easy Auth in front, not a plain
`x-api-key` header.

---

## Identity scope (what each client sees)

Key-only clients (Copilot CLI, VS Code, Claude Code/Desktop, Cursor, curl) authenticate with the
**shared `x-api-key`**, so every request acts as the deployment's **`service_account`** Tableau
identity and sees exactly what that account's RLS allows — they do **not** carry per-user Entra
identity. Per-user RLS (`passthrough`) requires Entra Easy Auth in front of the endpoint **plus** an
identity-carrying caller (such as an M365 Copilot / Copilot Studio agent with Easy Auth); a key-only
client can't supply that. Scope the service account least-privilege accordingly. See
[identity-modes.md](identity-modes.md).

## Secret discipline

- **User-global** configs (`~/.copilot/mcp-config.json`, `~/.claude.json`, `~/.cursor/mcp.json`,
  `claude_desktop_config.json`, VS Code user config) live outside your repository. The key may sit
  there locally, but still treat it as a secret — restrict file permissions and never share or
  screenshot it.
- **Project-scoped** configs (`.mcp.json`, `.cursor/mcp.json`, `.vscode/mcp.json`, a repo
  `.copilot/mcp-config.json`) can be committed, so **never hardcode the key** in them. Use the
  client's prompted input / env interpolation (VS Code `${input:...}`, Cursor `${env:...}`, the
  `mcp-remote` `env` block) or keep the server entry in user-global config instead.
- Rotate the key via the `sidecar-api-key` Container App secret — see
  [security-operations.md](security-operations.md).

## Security

Anyone with the **endpoint URL and the API key** can query Tableau as the service account. Treat the
key as a secret, rotate it on a schedule and on suspected exposure, and prefer **Entra Easy Auth +
`passthrough`** for per-user identity. For any client exposed beyond your control, front the endpoint
with Easy Auth/OAuth rather than relying on the shared key alone.

## Verify it works

1. Health: `curl -s https://<app>.<region>.azurecontainerapps.io/healthz` → `{"status":"ok", ...}`.
2. In the client, ask:
   - *"What Tableau datasources can you see?"* → `list-datasources`
   - *"What fields are in the Superstore datasource?"* → `get-datasource-metadata`
   - *"What were the top 3 regions by total sales?"* → `query-datasource`

The agent should call the tools and answer from live Tableau data.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Tools don't appear | Confirm the server is connected (`/mcp show` in Copilot CLI, `/mcp` in Claude Code, **Configure Tools** in VS Code, **Settings → MCP** in Cursor, hammer icon in Claude Desktop); check the URL ends in `/mcp` and the key is right. |
| `401` from the server | The API key is wrong or not sent as `x-api-key` (or `Authorization: Bearer <key>`). |
| First call hangs a few seconds | The Container App scaled to zero; the first request after idle is a cold start. Retry. |
| Empty / partial results | In `service_account` mode the account's RLS may legitimately limit rows. |
| Claude Desktop won't connect | Check Node 18+; use `--transport http-only` and the no-space `--header`; clear `~/.mcp-auth` and restart. |
| M365 Copilot agent won't call tools | Generative orchestration must be ON; the connection's API key must match the deployed `sidecarApiKey`. |
