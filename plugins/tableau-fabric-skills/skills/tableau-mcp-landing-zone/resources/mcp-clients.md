# Workflow: Wire the endpoint into code-running Copilots (Copilot CLI / Claude Code / Cursor)

Connect a code-executing Copilot to the deployed (or local) Tableau MCP endpoint so an agent can
list datasources, read metadata, and run natural-language queries against **live Tableau data**.
Tools are discovered automatically over MCP; you do **not** declare each tool by hand.

This covers the three code-running clients this collection targets:
**GitHub Copilot CLI**, **Claude Code**, and **Cursor**. To wire a Copilot Studio agent instead,
see [copilot-studio-wiring.md](copilot-studio-wiring.md).

## What you need

| Value | Where it came from |
|---|---|
| **MCP endpoint** | Azure deploy: the `mcpEndpoint` output, e.g. `https://<app>.<region>.azurecontainerapps.io/mcp`. Local stack: `http://localhost:9000/mcp` (see [local-dev.md](local-dev.md)). |
| **API key** | The `sidecarApiKey` you set at deploy time (local: `SIDECAR_API_KEY` from `.env`). The shared secret every caller must send. |

All three clients connect the same way: **Streamable HTTP** transport, with the key in the
**`x-api-key`** request header. If a client only offers an `Authorization` field, use
`Authorization: Bearer <API key>` instead - the sidecar accepts either.

The curated tool set the landing zone ships is `list-datasources`, `get-datasource-metadata`,
`query-datasource`, and `search-content` (see the tool-curation note in
[copilot-studio-wiring.md](copilot-studio-wiring.md)).

## GitHub Copilot CLI

**Interactive:** run `/mcp add`, then fill the form:

- **Server Name** `tableau`
- **Server Type** `HTTP` (Streamable HTTP)
- **URL** your MCP endpoint (ends in `/mcp`)
- **HTTP Headers** `{"x-api-key": "YOUR-SIDECAR-API-KEY"}`
- **Tools** `*`

Save with <kbd>Ctrl</kbd>+<kbd>S</kbd>; it is available immediately. Manage with `/mcp show`,
`/mcp show tableau`, `/mcp edit tableau`, `/mcp delete tableau`.

**Config file** - `~/.copilot/mcp-config.json` (Windows: `%USERPROFILE%\.copilot\mcp-config.json`):

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

## Claude Code

**CLI:**

```bash
claude mcp add --transport http tableau \
  https://<app>.<region>.azurecontainerapps.io/mcp \
  --header "x-api-key: YOUR-SIDECAR-API-KEY"
```

Add `--scope user` to make it available in every project, or `--scope project` to write a shared
`.mcp.json` (if you do, do **not** hardcode the key - see Secret discipline below).

**Config file** - project `.mcp.json` or user `~/.claude.json`:

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

In JSON, `type` accepts `streamable-http` as an alias for `http`, so configs copied from server
docs work unchanged.

## Cursor

**Config file** - global `~/.cursor/mcp.json` or project `.cursor/mcp.json`:

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
**Settings -> MCP**.

## Local stack instead of Azure

To point any of the three at the local docker-compose stack ([local-dev.md](local-dev.md)), use
`http://localhost:9000/mcp` as the URL and your `SIDECAR_API_KEY` as the `x-api-key` value.
Confirm the stack first: `curl -s http://localhost:9000/healthz` should return `{"status":"ok", ...}`.

## Secret discipline

- **User-global** configs (`~/.copilot/mcp-config.json`, `~/.claude.json`, `~/.cursor/mcp.json`)
  live outside your repository. The key may sit there locally, but still treat it as a secret -
  restrict file permissions and never share or screenshot it.
- **Project-scoped** configs (`.mcp.json`, `.cursor/mcp.json`, a repo `.copilot/mcp-config.json`)
  can be committed, so **never hardcode the key** in them. Use env interpolation where the client
  supports it (Cursor `${env:...}`, Claude Code `${VAR}`), or keep the server entry in user-global
  config instead. Rotate the key via the `sidecar-api-key` Container App secret
  ([security-operations.md](security-operations.md)).

## Identity scope (what these clients see)

These clients authenticate to the sidecar with the **shared `x-api-key`**, so every request acts as
the deployment's **`service_account`** Tableau identity and sees exactly what that account's RLS
allows - they do **not** carry per-user Entra identity. Per-user RLS (`passthrough`) requires Entra
Easy Auth in front of the endpoint plus an identity-carrying caller (such as a Copilot Studio agent
with Easy Auth); a key-only CLI client cannot supply that. Scope the service account
least-privilege accordingly. See [identity-modes.md](identity-modes.md).

## Verify it works

1. Health: `curl -s https://<app>.<region>.azurecontainerapps.io/healthz` -> `{"status":"ok", ...}`.
2. In the client, ask:
   - "What Tableau datasources can you see?" -> `list-datasources`
   - "What fields are in the Superstore datasource?" -> `get-datasource-metadata`
   - "What were the top 3 regions by total sales?" -> `query-datasource`

The agent should call the tools and answer from live Tableau data.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Tools don't appear | Confirm the server is connected (`/mcp show` in Copilot CLI, `/mcp` in Claude Code, **Settings -> MCP** in Cursor); check the URL ends in `/mcp` and the key is right. |
| `401` from the server | The API key is wrong or not sent as `x-api-key` (or `Authorization: Bearer <key>`). |
| First call hangs a few seconds | The Container App scales to zero; the first request after idle is a cold start. Retry. |
| Empty / partial results | In `service_account` mode the account's RLS may legitimately limit rows. |
