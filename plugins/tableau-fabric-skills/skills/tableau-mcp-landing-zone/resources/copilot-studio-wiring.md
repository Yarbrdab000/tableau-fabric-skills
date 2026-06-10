# Workflow: Wire the endpoint into Microsoft Copilot Studio

Connects the deployed landing zone to a Copilot Studio agent so business users can ask
natural-language questions about Tableau datasources. Tools are discovered automatically over
MCP — you do **not** define each action by hand.

You need two values from the deploy step:

| Value | Where it came from |
|---|---|
| **MCP endpoint** | the `mcpEndpoint` output, e.g. `https://<app>.<region>.azurecontainerapps.io/mcp` |
| **API key** | the `sidecarApiKey` you set when deploying (the shared secret callers must send) |

## Prerequisite — generative orchestration ON

In Copilot Studio open the agent → **Settings → Generative AI → Orchestration = generative**.
**MCP tools are ignored under classic orchestration.**

## Option A — Import the custom connector (recommended, most reliable)

Uses [`assets/copilot-studio/mcp-connector.swagger.yaml`](../assets/copilot-studio/mcp-connector.swagger.yaml).

1. Open that swagger file.
2. Edit one line — set `host:` to **your** Container App FQDN (the `mcpEndpoint` **without**
   `https://` and **without** the trailing `/mcp`). E.g. for
   `https://tableau-mcp.graysea-5a3f72c8.westus3.azurecontainerapps.io/mcp`, host is
   `tableau-mcp.graysea-5a3f72c8.westus3.azurecontainerapps.io`.
3. Go to **Power Apps** (<https://make.powerapps.com>) → pick the **same environment** your agent
   uses → **More → Discover all → Custom connectors** (or via a Solution).
4. **New custom connector → Import an OpenAPI file** → upload the edited swagger → name it
   `Tableau MCP` → **Continue**.
5. On the **Security** tab confirm: **API Key**, label `x-api-key`, name `x-api-key`, location
   **Header** → **Create connector**.
6. **Test / + New connection** → paste your **API key** when prompted.

Then add it to the agent:

7. Copilot Studio → your agent → **Tools** (or **Actions**) → **+ Add a tool**.
8. Find **Tableau MCP** (Model Context Protocol) → **Add to agent**. Copilot connects and lists the
   curated tools (`list-datasources`, `get-datasource-metadata`, `query-datasource`, `search-content`).

## Option B — Built-in MCP tool (if your tenant has it)

1. Copilot Studio → agent → **Tools → + Add a tool → New tool → Model Context Protocol**.
2. Server name `Tableau MCP`; **Server URL** = your MCP endpoint (ends in `/mcp`); Transport
   **Streamable HTTP**.
3. Authentication **API key** → Header name `x-api-key` → value = your API key. (If only
   `Authorization` is offered, use value `Bearer <your API key>`.)
4. **Create → Add to agent**.

## Test it

In the agent's **Test** pane:

- "What Tableau datasources can you see?" → `list-datasources`
- "What fields are in the Superstore datasource?" → `get-datasource-metadata`
- "What were the top 3 regions by total sales?" → `query-datasource` (expect West / East / Central)

The agent should call the tools and answer from **live Tableau data**.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Tools don't appear / agent won't call them | Confirm generative orchestration is ON and the connection's API key matches the deployed `sidecarApiKey`. |
| `401` from the server | The API key is wrong or not sent in `x-api-key` (or `Authorization: Bearer <key>`). |
| Cold start delay | The Container App scales to zero; the first request after idle takes a few seconds. |
| Empty/partial results | In `service_account` mode the account's RLS may limit rows; in `passthrough` the signed-in user may legitimately have none. |

## Notes

- **Tool curation:** the landing zone ships `includeTools=datasource,content-exploration` and
  `maxResultLimits=query-datasource:100`, trimming the official server's ~20 tools to the
  high-signal NL-query set with a sane row cap. Widen by editing those parameters at deploy time
  (e.g. add the `pulse` or `view` group). Fewer, well-described tools orchestrate more reliably.
- **Access model:** `service_account` → all agent users see what that one account sees (scope it
  least-privilege). For per-user RLS use `passthrough` + Easy Auth — see
  [identity-modes.md](identity-modes.md).
- **Security:** anyone with the endpoint URL **and** the API key can query. Treat the key as a
  secret and rotate it via the `sidecar-api-key` Container App secret — see
  [security-operations.md](security-operations.md).
