# Wire the Tableau MCP server into Microsoft Copilot Studio

This connects your deployed landing zone (an Azure Container App running the **official
Tableau MCP server** behind our auth sidecar) to a Copilot Studio agent so business users
can ask natural-language questions about your Tableau datasources. Tools are discovered
automatically over MCP — you do **not** define each action by hand.

You need two values from the Deploy-to-Azure step:

| Value | Where it came from |
| --- | --- |
| **MCP endpoint** | the `mcpEndpoint` output, e.g. `https://<app>.<region>.azurecontainerapps.io/mcp` |
| **API key** | the `sidecarApiKey` you set when deploying (the shared secret callers must send) |

There are two supported paths. **Option A (custom connector)** is the most reliable
and is what `mcp-connector.swagger.yaml` in this folder is for. **Option B** uses
Copilot Studio's built-in "Add a tool" MCP flow if your tenant has it.

---

## Prerequisites

- A Copilot Studio agent (or create one at <https://copilotstudio.microsoft.com>).
- **Generative orchestration must be ON** for the agent
  (Agent → **Settings** → **Generative AI** → *Orchestration* = generative). MCP tools
  are ignored under classic orchestration.

---

## Option A — Import the custom connector (recommended)

1. Open the swagger file in this folder: **`mcp-connector.swagger.yaml`**.
2. Edit one line — set `host:` to **your** Container App FQDN (the `mcpEndpoint`
   without `https://` and without the trailing `/mcp`). For example, for
   `https://tableau-mcp.<your-env>.westus3.azurecontainerapps.io/mcp`, the host is
   `tableau-mcp.<your-env>.westus3.azurecontainerapps.io`.
3. Go to **Power Apps** → <https://make.powerapps.com> → pick the same environment your
   agent uses → **More** → **Discover all** → **Custom connectors**
   (or **Solutions** → your solution → **New** → **Automation** → **Custom connector**).
4. Choose **New custom connector** → **Import an OpenAPI file** → upload the edited
   `mcp-connector.swagger.yaml` → name it `Tableau MCP` → **Continue**.
5. On the **Security** tab confirm: **API Key**, parameter label `x-api-key`,
   parameter name `x-api-key`, location **Header**. → **Create connector**.
6. Click **Test** (or **+ New connection**). When prompted for the API key, paste your
   **MCP Api Key**. (Listed under *Connections* afterward.)

Then add it to the agent:

7. In Copilot Studio open your agent → **Tools** (or **Actions**) → **+ Add a tool**.
8. Find **Tableau MCP** (Model Context Protocol) → **Add to agent**. Copilot connects to
   the server and lists the tools (with the recommended curation: `list-datasources`,
   `get-datasource-metadata`, `query-datasource`, `search-content`).

---

## Option B — Built-in MCP tool (if available in your tenant)

1. Copilot Studio → your agent → **Tools** → **+ Add a tool** →
   **New tool** → **Model Context Protocol**.
2. Server name: `Tableau MCP`. Server URL: your **MCP endpoint** (ends in `/mcp`).
   Transport: **Streamable HTTP**.
3. Authentication: **API key** → Header name `x-api-key` → value = your **API key**.
   (If only `Authorization` is offered, use value `Bearer <your API key>`.)
4. **Create** → **Add to agent**.

---

## Test it

In the agent's **Test** pane, ask things like:

- "What Tableau datasources can you see?"  → calls `list-datasources`.
- "What fields are in the Superstore datasource?"  → calls `get-datasource-metadata`.
- "What were the top 3 regions by total sales?"  → calls `query-datasource`
  (you should get West / East / Central).

The agent should call the tools and answer from live Tableau data.

---

## Notes & troubleshooting

- **Tools don't appear / agent won't call them:** confirm generative orchestration is ON
  and the connection's API key matches the deployed `sidecarApiKey`.
- **401 from the server:** the API key is wrong or not being sent in `x-api-key`.
- **Cold start:** the Container App scales to zero; the first request after idle can take
  a few seconds while a replica spins up. Subsequent calls are fast.
- **Tool curation:** the landing zone ships `INCLUDE_TOOLS=datasource,content-exploration,workbook,view,pulse`
  and `MAX_RESULT_LIMITS=query-datasource:100` — the full NL-analytics set (data, content, workbooks, views, Pulse) with the
  default row cap. Trim those parameters at deploy
  time (e.g. drop `pulse`). Fewer, well-described tools = more reliable
  orchestration on weaker models.
- **Access model:** in the default `service_account` mode the server queries Tableau as the
  single configured account (`serviceAccountUsername`) — all agent users see what that account
  can see, so scope it with least privilege. For **per-user row-level security**, deploy with
  `identityMode=passthrough` + Easy Auth so each signed-in M365 user's Entra identity is mapped
  to their Tableau user. See `../../docs/customer-setup-guide.md`.
- **Security:** anyone with the endpoint URL **and** the API key can query. Treat the key
  as a secret; rotate it by updating the `sidecar-api-key` Container App secret and the
  connector connection. For stronger auth, enable Microsoft Entra (Easy Auth) in front (see the
  setup guide).
