# Workflow: Wire the endpoint into Microsoft Copilot Studio

Connects the deployed landing zone to a Copilot Studio agent so business users can ask
natural-language questions about Tableau datasources. Tools are discovered automatically over
MCP — you do **not** define each action by hand.

> **Consuming from a code client instead?** GitHub Copilot CLI, VS Code, Claude, Cursor, or raw curl
> wire to the same endpoint a different way — see [`mcp-clients.md`](mcp-clients.md). This doc is the
> **Copilot Studio / Teams / Microsoft 365 Copilot** path.

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

> **Tip:** if you deployed with `deploy.ps1`, it already wrote `mcp-connector.generated.swagger.yaml`
> (in the folder you ran it from) with `host:` **pre-filled** from your `mcpEndpoint` — import that
> file and skip the manual edit in step 2.

1. Open that swagger file.
2. Edit one line — set `host:` to **your** Container App FQDN (the `mcpEndpoint` **without**
   `https://` and **without** the trailing `/mcp`). E.g. for
   `https://tableau-mcp.<your-env>.westus3.azurecontainerapps.io/mcp`, host is
   `tableau-mcp.<your-env>.westus3.azurecontainerapps.io`.
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

## Publish to Microsoft 365 Copilot

This is the **live-test path**: once the agent answers correctly in the Test pane, publish it so people
can use it from **Microsoft 365 Copilot** (Copilot Chat). The flow is maker → publish → channel →
admin approval → end users. UI labels in Copilot Studio and the admin centers **shift frequently** —
treat the bold names below as a guide and **confirm against the linked Microsoft docs for your tenant**.

1. **Publish the agent.** Copilot Studio → your agent → **Publish**. You must publish at least once
   before the agent can be added to any channel.
2. **Add the "Teams and Microsoft 365 Copilot" channel.** Open the agent's **Channels** →
   **Teams and Microsoft 365 Copilot** → in the configuration panel, under **Turn on Microsoft 365**,
   confirm **"Make agent available in Microsoft 365 Copilot"** is selected → **Add channel**. If you
   leave that option off, the agent is added to **Teams only**, not Microsoft 365 Copilot.
3. **Install for yourself first — self-serve, no admin needed (enough for your live test).** In the same panel select
   **See agent in Teams** — with the Microsoft 365 option on, this installs to **both** Teams and
   Microsoft 365 Copilot. In Microsoft 365 Copilot, type **`@`**, pick your agent, and ask a question.
4. **Submit for organization-wide use (requires a Teams/M365 admin — NOT self-serve).** To reach other users, open the channel panel →
   **Availability options** → **Show to everyone in my org** → review the requirements →
   **Submit for admin approval**. Remove the agent from **Show to my teammates and shared users** first,
   or it can appear in two places. Submitting routes the agent to your tenant admin; if the agent is
   also published to Microsoft 365, the same submission covers the **Microsoft 365 Agent Store**.
5. **Admin approves and deploys.** A tenant admin (Teams / Microsoft 365 admin role) approves and
   manages the agent. Approved agents are featured in **Built for your org** (Teams app store) /
   **Built by your org** (Microsoft 365 Agent Store). Admins enable, assign, block, or remove agents
   from the **Microsoft 365 admin center** under **agent management** (historically **Integrated Apps**;
   some tenants now surface a dedicated **Agents** area), and approve the app listing from the
   **Teams admin center → Manage apps**. The exact surface is tenant-dependent — **confirm for your
   tenant**.
6. **Licensing.** Each end user who interacts with the agent in Microsoft 365 Copilot needs a
   **Microsoft 365 Copilot license**. (Agent management is enabled by default in Copilot-licensed
   tenants.)
7. **Where it shows up.** Users find the agent in **Microsoft 365 Copilot (Copilot Chat) → Agents**, or
   by typing **`@`** and selecting it, then asking in natural language (e.g.
   *"@Tableau MCP what were the top 3 regions by total sales?"*).

**Identity caveat (same as the code clients):** the agent authenticates to the MCP endpoint with the
single **`x-api-key`** (a `service_account`-scoped key), so **every** Microsoft 365 Copilot user
querying through this agent shares that **one** Tableau service-account identity — it is **not**
per-user. For per-user row-level security, deploy `identityMode=passthrough` + Easy Auth and turn on
the agent's **end-user authentication** so each signed-in Entra identity flows to Tableau; see
[identity-modes.md](identity-modes.md).

**Docs (confirm current UI):** [Connect an agent to Teams and Microsoft 365 Copilot](https://learn.microsoft.com/en-us/microsoft-copilot-studio/publication-add-bot-to-microsoft-teams)
· [Manage agents in the Microsoft 365 admin center](https://learn.microsoft.com/en-us/microsoft-365/admin/manage/manage-copilot-agents-integrated-apps)

## Troubleshooting

| Symptom | Fix |
|---|---|
| Tools don't appear / agent won't call them | Confirm generative orchestration is ON and the connection's API key matches the deployed `sidecarApiKey`. |
| `401` from the server | The API key is wrong or not sent in `x-api-key` (or `Authorization: Bearer <key>`). |
| Cold start delay | The Container App scales to zero; the first request after idle takes a few seconds. |
| Empty/partial results | In `service_account` mode the account's RLS may limit rows; in `passthrough` the signed-in user may legitimately have none. |

## Notes

- **Tool curation:** the landing zone ships `includeTools=datasource,content-exploration,workbook,view,pulse` and
  `maxResultLimits=query-datasource:100` — the full NL-analytics set (data, content, workbooks, views, Pulse) with the
  default row cap. Trim those parameters at deploy time
  (e.g. drop `pulse`). Fewer, well-described tools orchestrate more reliably on weaker models.
- **Access model:** `service_account` → all agent users see what that one account sees (scope it
  least-privilege). For per-user RLS use `passthrough` + Easy Auth — see
  [identity-modes.md](identity-modes.md).
- **Security:** anyone with the endpoint URL **and** the API key can query. Treat the key as a
  secret and rotate it via the `sidecar-api-key` Container App secret — see
  [security-operations.md](security-operations.md).
