---
name: tableau-mcp-landing-zone
description: >-
  Deploy the official Tableau MCP server (ghcr.io/tableau/tableau-mcp) behind a
  Microsoft auth sidecar so business users can ask natural-language questions about
  Tableau data from Copilot Studio / M365 Copilot / Azure AI Foundry. Provides a
  one-click Deploy to Azure (Container Apps, HTTPS, scale-to-zero) or a deploy.ps1
  path, an x-api-key front door for Copilot Studio connectors, optional
  Entra-to-Tableau identity passthrough for per-user row-level security, and a local
  docker-compose harness. Use to deploy the Tableau MCP server on Azure, connect
  Tableau to Copilot Studio, enable per-user RLS, or run it locally for evaluation.
  Triggers: "deploy tableau mcp", "tableau mcp on azure", "tableau mcp server",
  "natural language tableau in copilot", "connect tableau to copilot studio",
  "tableau row level security copilot", "tableau mcp passthrough", "tableau mcp landing zone".
---

> **AUTH MODEL — tableau-mcp-landing-zone**
> Tableau **Connected App (Direct Trust)** at the auth sidecar, with an `x-api-key` front door for
> callers. Entra → Tableau **passthrough** is the secure default; `service_account` is an explicit
> fallback only — **never auto-downgrade** from a failed passthrough.

> **CRITICAL — credentials & identity are a security boundary.**
> This skill emits **deployment parameters and configuration only**. It never stores or commits
> a Tableau Connected App secret or `sidecarApiKey`. The user creates the Connected App, supplies
> the four secret values at deploy time, and controls who can call the endpoint. On any
> credential / sign-in error, **stop and have the user fix the Connected App or key** — never
> weaken auth (e.g. `service_account` is a *fallback for convenience*, never an auto-downgrade
> from a failed `passthrough`).

# Tableau MCP on Microsoft — Landing-Zone Deployment Skill

This skill packages **Play 1** of the Tableau → Fabric bridge as a reusable agent skill. Its job
is **procedural**: stand up the official Tableau MCP server in the user's Azure tenant behind an
auth sidecar, wire it into Microsoft Copilot, and operate it — so a business user can ask
*"what were total sales by region in Superstore?"* in Copilot and get a live answer from Tableau.

**We wrap, not fork.** The official image (`ghcr.io/tableau/tableau-mcp`) runs unmodified, so the
deployment inherits Tableau's ongoing updates and its full supported tool set (datasources, VizQL
Data Service queries, workbooks, views, Pulse, content search — ~20 tools; the default exposes the full NL-analytics set, trimmable per deploy).

## Architecture

```mermaid
flowchart LR
  U[Business user] -->|natural language| C[Microsoft Copilot]
  C -->|MCP over HTTPS /mcp<br/>x-api-key or Entra| S[Auth sidecar<br/>public ingress]
  S -->|localhost + X-Tableau-Auth| M[Official Tableau MCP<br/>internal only]
  M -->|REST / Metadata / VizQL Data Service / Pulse| T[(Tableau Cloud / Server)]
  S -. signs per-user Connected App JWT .-> T
```

Both containers run in **one** Azure Container App. Only the sidecar is exposed; the official
server listens on localhost, so the sidecar is the complete auth boundary (which is why the
official server's `DANGEROUSLY_DISABLE_OAUTH=true` is safe).

## Deployable assets — vendored in `assets/` (self-contained)

This skill is the **navigator/operator**, and it ships a self-contained copy of the deploy bundle
under [`assets/`](assets/) so you can deploy without cloning anything else. Paths below are relative
to this skill folder. The **heavy sidecar source** is *not* vendored — it ships as a prebuilt image, so
you do **not** need it (or any other repo) to deploy or run this skill; it's listed (last row) only for
developers who want to modify the sidecar.

| Asset | Path | Purpose |
|---|---|---|
| Azure landing zone (Bicep) | [`assets/azure/main.bicep`](assets/azure/main.bicep) | Container App + sidecar + official image, identity wiring, optional Key Vault / Easy Auth. |
| Portal template (compiled) | [`assets/azure/azuredeploy.json`](assets/azure/azuredeploy.json) | Backs the **Deploy to Azure** button. |
| Parameters template | [`assets/azure/main.parameters.json`](assets/azure/main.parameters.json) | Param shape (placeholders only — **never** put secrets here). |
| CLI deploy | [`assets/azure/deploy.ps1`](assets/azure/deploy.ps1) | `az`-based deploy (PAT-free; prints `mcpEndpoint` + `healthUrl`). |
| Local harness | [`assets/local/docker-compose.yml`](assets/local/docker-compose.yml) + [`.env.example`](assets/local/.env.example) | Official image behind the **published** sidecar image for evaluation. |
| Copilot Studio connector | [`assets/copilot-studio/mcp-connector.swagger.yaml`](assets/copilot-studio/mcp-connector.swagger.yaml) + [`README.md`](assets/copilot-studio/README.md) | Custom-connector swagger + wiring guide. |
| Deploy verifier | [`scripts/verify_deployment.py`](scripts/verify_deployment.py) | Stdlib fail-loud check: health + auth-enforced + MCP handshake. |
| Auth sidecar (source + 31 tests) | bridge repo → [`Play1/sidecar/`](https://github.com/Yarbrdab000/Tableau-Fabric-AI-Bridge/tree/main/Play1/sidecar) | Starlette reverse proxy (`proxy.py`, `identity.py`, `config.py`, `tests/`). Built/published as the sidecar image; clone only to develop or test the source. |

> **Provenance:** the `assets/` bundle is **self-contained and maintained in this skill** — it is the
> source of truth for the deploy infra here; nothing is synced from another repository. See
> [`assets/README.md`](assets/README.md).

> **Out of scope:** the *no-MCP* route (a Logic App + OpenAPI tool, in the bridge repo's
> `Play1_no_MCP/`) is a separate connectivity option and is **not** this skill. If the user wants the
> no-code/Logic-App path, point them at the bridge repo instead.

## Prerequisites — establish these FIRST

Before deploying, confirm the user has (ask if unknown — do not assume):

1. **A Tableau Connected App (Direct Trust)** on their Tableau Cloud/Server site, **Enabled**,
   with scopes `tableau:content:read` + `tableau:viz_data_service:read` (plus `tableau:views:download` and the Pulse insight
   scopes for the default view + Pulse tools — see *Scopes by capability* in [identity-modes.md](resources/identity-modes.md)). This yields **Client ID**, **Secret ID**, **Secret Value** (shown once),
   and the **site content URL** (slug). See [identity-modes.md](resources/identity-modes.md).
2. **An Azure subscription** + permission to create a resource group / Container Apps.
3. **A target identity mode** — `service_account` (default, simplest) or `passthrough` (per-user
   RLS). Choose deliberately; see the Identity Modes table below.
4. For Copilot wiring: a **Copilot Studio** environment (or M365 Copilot with agent extensibility)
   with **generative orchestration ON** (MCP tools are ignored under classic orchestration).

If the user only says "set up Tableau in Copilot" without specifics, **ask which identity mode**
and whether they want **Azure** (production) or **local docker-compose** (evaluation) first.

## Information to collect (intake checklist)

Gather the **non-secret** values below first — when driving an interactive deploy the agent
may ask for these one at a time. The **two secrets** (the Connected App **Secret Value**, item 5,
and the **Sidecar Api Key**, item 9) are different: they follow the *Secret intake* rule below and
**must never be typed into chat**.

> **🔴 Secret intake — non-negotiable: never collect a secret in chat.**
> A secret must never pass through the chat transcript, the model, a tool argument the agent
> echoes, or a committed file. **Do not ask the user to paste the Connected App Secret Value or
> the Sidecar Api Key**, and do not paste them yourself. Instead:
>
> 1. The user stores each secret **once, out of band, in their own shell** — in Azure Key
>    Vault (preferred) or a local environment variable:
>
>    ```powershell
>    az keyvault secret set --vault-name <your-vault> --name tableau-ca-secret-value --value <secret>
>    ```
>
> 2. At deploy time the secret is read into a local variable and passed straight to the deploy, so
>    its value is never printed or stored in chat:
>
>    ```powershell
>    $secret = az keyvault secret show --vault-name <your-vault> --name tableau-ca-secret-value --query value -o tsv
>    ./deploy.ps1 -ResourceGroup <rg> ... -ConnectedAppSecretValue $secret
>    ```
>
> **If a secret is ever pasted into chat anyway, STOP:** tell the user that value is now burned and
> have them **rotate it** (generate a new Connected App secret / a new Sidecar Api Key) before
> continuing, then supply the new value via Key Vault. Never deploy with a secret that appeared in
> the transcript.

> **Three distinct values from the same Connected App — don't conflate them (items 3–5):**
> - **Client ID** — the app's *identity* (the JWT `iss`). **Not** secret.
> - **Secret ID** — *names* which generated secret you're using (the JWT header `kid`). **Not** secret.
> - **Secret Value** — the actual **signing secret**, shown once. 🔴 Secret — Key Vault only.
>
> **Recommended Key Vault secret names** (greppable, not opaque GUIDs):
> `tableau-ca-client-id`, `tableau-ca-secret-id`, `tableau-ca-secret-value`, `tableau-sidecar-api-key`.
> Client ID and Secret ID aren't secret, but keeping all four together in one vault is tidy and auditable.
>
> **No Tableau password is ever needed.** Connected Apps authenticate with a **signed JWT**, not a
> password — the service account (item 7) is referenced by **username only**. If anyone asks for that
> account's password, that's a red flag: stop and reconsider.

> **Required vs optional.** Items **1–5, 7, 8, 9, 10, 11** are needed for **every** deploy; item **6**
> (scopes) is set on the Connected App, not passed to the deploy. The final **passthrough-only** row
> applies only when you choose `identityMode=passthrough`.

| # | Value | How to get it | Example |
|---|---|---|---|
| 1 | **Tableau pod URL** (`tableauServer`) | The host of your Tableau Cloud/Server URL (everything before `/#/site/...`). | `https://10ay.online.tableau.com` |
| 2 | **Site content URL** (`tableauSite`) | The slug after `/site/` in your Tableau URL. Default site = empty string. | `acme-analytics` |
| 3 | **Connected App Client ID** | Tableau → **Settings → Connected Apps → New Connected App → Direct Trust**; create it, set **Enabled**. Shown on the app. | `a1b2c3d4-…` |
| 4 | **Connected App Secret ID** | On the same app, **Generate New Secret** → copy the Secret ID. | `e5f6…` |
| 5 | **Connected App Secret Value** | Copied from the same **Generate New Secret** step — **shown once**. *(secret)* | 🔴 **never paste in chat** — store in Key Vault / env var (see *Secret intake*) |
| 6 | **Scopes enabled** | Base on the Connected App: `tableau:content:read` + `tableau:viz_data_service:read` (covers data queries, content search, workbooks). The default set also enables **views** + **Pulse** — grant `tableau:views:download` and the 5 Pulse scopes for those (until then they 401; server stays healthy), or trim `includeTools`. `.tdsx` download for the sibling skills needs `tableau:datasources:download`. See the *Scopes by capability* table in [identity-modes.md](resources/identity-modes.md). | — |
| 7 | **Service account username** (`serviceAccountUsername`) | A **least-privilege** Tableau user the agent acts as (in `service_account` mode every Copilot user queries as this user). Not a Site Admin in prod. | `svc-mcp@acme.com` |
| 8 | **Identity mode** (`identityMode`) | `service_account` (default) or `passthrough` (per-user RLS). See [identity-modes.md](resources/identity-modes.md). | `service_account` |
| 9 | **Sidecar Api Key** (`sidecarApiKey`) | **Invent** a long random string (e.g. a GUID). You'll paste it into Copilot Studio later. PowerShell: `(New-Guid).Guid`. *(secret)* | 🔴 **never paste in chat** — store in Key Vault / env var (see *Secret intake*) |
| 10 | **Azure subscription** | Confirm `az account show`; switch with `az account set --subscription <id>` if needed. | — |
| 11 | **Resource group** + **region** | An existing RG or one to create (`az group create -n <rg> -l <region>`). | `my-rg`, `eastus` |
| _passthrough only_ | **Entra tenant id / client id** (`entraTenantId`, `entraClientId`) + **UPN mapping** (`upnMappingMode` + domains) | Pre-create an Entra app registration for Easy Auth; pick `direct`/`transform`/`explicit` mapping. See [identity-modes.md](resources/identity-modes.md). | `<tenant-guid>`, `<app-guid>` |

> **Verify the inputs work before standing up Azure (optional but recommended):** the
> `tableau-datasource-profiler` skill uses the *same* Connected App (Direct Trust JWT) — a quick
> profile of one datasource confirms the Client ID / Secret / scopes are valid before you deploy.

## Workflow Selector

| The user wants to… | Workflow | Resource |
|---|---|---|
| Deploy to Azure (button or CLI), get the MCP endpoint | **Deploy the landing zone** | [deploy-azure.md](resources/deploy-azure.md) |
| Choose / configure `service_account` vs per-user-RLS `passthrough`, map Entra UPN → Tableau user | **Configure identity** | [identity-modes.md](resources/identity-modes.md) |
| Register the endpoint in Copilot Studio and test NL queries | **Wire into Copilot Studio** | [copilot-studio-wiring.md](resources/copilot-studio-wiring.md) |
| Consume the deployed endpoint from a client — M365 Copilot, Copilot CLI, VS Code, Claude Code/Desktop, Cursor, or curl (**ask the user which**) | **Consume the endpoint** | [mcp-clients.md](resources/mcp-clients.md) |
| Run the real stack locally for evaluation; run sidecar tests | **Local dev / evaluate** | [local-dev.md](resources/local-dev.md) |
| Harden with Entra Easy Auth, rotate the API key, curate tools, troubleshoot | **Secure & operate** | [security-operations.md](resources/security-operations.md) |

## Identity Modes

| `identityMode` | What each agent user sees | Per-user RLS | Requirements |
|---|---|---|---|
| `service_account` (default) | Everything the one configured Tableau account can see | No | Works in any tenant; no Entra wiring. Use a **least-privilege** Tableau user (a Site Admin bypasses RLS). |
| `passthrough` | Only the rows *their own* Tableau user may see | **Yes** | Easy Auth (or APIM) in front + a UPN → Tableau username mapping. **Fail-closed**: unmapped callers are denied, never downgraded to the service account. |

## Key deployment parameters

Full list is in [`assets/azure/main.bicep`](assets/azure/main.bicep).

| Parameter | Purpose |
|---|---|
| `tableauServer` / `tableauSite` | Tableau pod URL + site content URL (slug). |
| `connectedAppClientId` / `connectedAppSecretId` / `connectedAppSecretValue` | Connected App (Direct Trust). Secret values are entered at deploy time, never committed. |
| `serviceAccountUsername` | Tableau user the service account acts as (required by the official server at startup; the identity used in `service_account` mode). |
| `allowApiKey` / `sidecarApiKey` | Enable + set the shared `x-api-key` for Copilot Studio. |
| `identityMode` | `service_account` (default) or `passthrough`. |
| `upnMappingMode` (+ `upnDomainFrom`/`upnDomainTo`) | Entra UPN → Tableau username (`direct` / `transform` / `explicit`). |
| `enableEasyAuth` (+ `entraClientId`, `entraTenantId`) | Microsoft Entra "Easy Auth" front door. |
| `useKeyVault` | Store secrets in Key Vault via managed identity instead of plain Container App secrets. |
| `includeTools` / `maxResultLimits` | Tool curation (default `datasource,content-exploration,workbook,view,pulse` + `query-datasource:100`; trim `includeTools` to slim the set). |
| `tableauMcpImage` / `sidecarImage` | Pinned image references (pin the official image by **digest** for production). |

Deploy **outputs** to capture: `mcpEndpoint` (register in Copilot), `healthUrl` (smoke check →
`{"status":"ok"}`), `identityModeOut`, `easyAuthEnabled`.

## Must / Prefer / Avoid

### MUST
- **Create + scope the Connected App before deploying.** Enable it; grant only the scopes needed.
- **Treat `sidecarApiKey` and the Connected App secret as secrets** — supply them at deploy time
  (portal form / CLI args) or via a **git-ignored** local parameters file; never commit them (not even
  in `assets/azure/main.parameters.json`), never echo them into the model/report or chat history, and
  never paste them into `az ... --debug` output.
- **Keep the official server internal-only.** Ingress targets the sidecar; the official container
  must have no public ingress. Never expose port 8000.
- **Fail closed in passthrough.** If a caller's UPN can't be mapped, the request is denied. Do not
  configure a silent fallback to the privileged service account.
- **Pin images by digest for production.** Deploy `tableauMcpImage` / `sidecarImage` as
  `…@sha256:<digest>`, not a moving tag, so a deploy can't silently pull a changed image.
- **Verify after deploy** — hit `healthUrl` (expect `{"status":"ok"}`) and run one NL query in
  Copilot's test pane before declaring success.

### PREFER
- **`service_account` for the first deploy / demo** (no Entra wiring), then graduate to
  `passthrough` once RLS is defined on the datasources and users exist on the Tableau site.
- **Least-privilege Tableau identities** — a least-privilege service account, and in passthrough,
  impersonated users that are **not** Site Admins (admins bypass RLS).
- **Right-size the tool set** (`includeTools` / `maxResultLimits`) — the default ships the full NL-analytics suite; trimming to fewer, well-described tools (and their scopes) orchestrates
  more reliably on weaker models.
- **Key Vault (`useKeyVault=true`) + Entra Easy Auth** for production hardening.

### AVOID
- **Do not fork or rebuild the official MCP image** — wrap it; you inherit Tableau's updates.
- **Do not trust client-supplied identity headers** — the sidecar strips `X-Tableau-Auth` /
  `X-MS-CLIENT-PRINCIPAL*`; only trust Easy Auth's principal when `TRUST_EASY_AUTH=true` behind a
  real gateway. (Locally, that header is spoofable — local passthrough is for testing only.)
- **Do not use a Site Admin as the service account** in production — it bypasses RLS and sees all data.
- **Do not confuse this with `Play1_no_MCP/`** (the Logic App route) or with the migration skills.

## Validation & Testing

- **Deploy verifier (recommended):** `python scripts/verify_deployment.py --base-url <mcpEndpoint without /mcp>`
  with the API key in `SIDECAR_API_KEY` — asserts `/healthz` ok, that unauthenticated `/mcp` is
  rejected, and that the MCP handshake lists tools, and prints whether Pulse is enabled. See
  [local-dev.md](resources/local-dev.md).
- **Smoke-query (stdlib):** `python scripts/query.py --base-url <mcpEndpoint> list` (key in
  `SIDECAR_API_KEY`) runs a real `list-datasources` over the SSE stream — use it instead of
  PowerShell `Invoke-WebRequest`. It won't guess a datasource: run `list`, then pass
  `--datasource <luid>` to `metadata` / `query`.
- **Sidecar tests (offline, no Docker) — _optional, sidecar-dev only; not required to deploy or use this
  skill._** The sidecar source + tests live in the bridge repo; clone it only if you're modifying the
  sidecar, then from `Play1/sidecar/` run `python -m pytest tests -q` (31 tests against an in-process
  mock upstream — auth, header stripping, identity mapping, proxy).
- **Local smoke (Docker):** from `assets/local/`, `docker compose up` then
  `curl -s localhost:9000/healthz`.
- **Azure smoke:** open `healthUrl` from the deployment outputs (expect `{"status":"ok"}`); then a
  Copilot test prompt that calls `list-datasources` / `query-datasource`.

## After deploy — consume the endpoint

Standing up the endpoint is only half the job; a user still has to consume it from a client. Once
`mcpEndpoint` is live and verified, **ask the user where they want to consume it** — Microsoft 365
Copilot, a Copilot Studio agent (Teams), GitHub Copilot CLI, VS Code Copilot, Claude Code, Claude
Desktop, Cursor, or a generic MCP client / curl — and then follow the matching section in
[mcp-clients.md](resources/mcp-clients.md) (Copilot Studio has its own deep-dive in
[copilot-studio-wiring.md](resources/copilot-studio-wiring.md)). **Do not guess** the client; route
to the one the user names. M365 Copilot additionally needs the agent published to the *Teams and
Microsoft 365 Copilot* channel and (for org-wide use) admin approval — that leg is covered in
[mcp-clients.md](resources/mcp-clients.md).

> **Grounding rules for the querying agent.** Once a client is wired up, the agent answering
> questions must **verify, not infer**: pick the datasource explicitly (ask when several could
> match), validate field names via `get-datasource-metadata`, confirm date boundaries with
> `MAX(date)` before any "latest/most recent" claim, and never assert data freshness or
> live-vs-extract without checking. Full checklist: **Query discipline & grounding** in
> [mcp-clients.md](resources/mcp-clients.md#query-discipline--grounding-agent-behavior).

## Reporting back to the user (close the loop in plain language)

The user should not see raw SSE frames, JSON-RPC, or `Invoke-WebRequest` noise. After a successful
deploy + verify, hand back a short, friendly summary instead of terminal output:

- **What's live:** "✅ Tableau MCP is deployed and reachable at `<mcpEndpoint>`."
- **What it can do now:** the **enabled tool set** (printed by `deploy.ps1` / `verify_deployment.py`),
  e.g. "4 tools: list datasources, read metadata, query data, search content." Name what's **off** and
  how to turn it on — e.g. "Pulse is off; say the word and I'll redeploy with the Pulse scopes."
- **Cold start:** "The first question after it's been idle may take ~15s while it wakes up" (so a slow
  first probe isn't mistaken for a broken deploy).
- **Cost & teardown:** "It scales to zero when idle (typically a few dollars/month); I can tear the
  whole thing down with one command when you're done" (see [deploy-azure.md](resources/deploy-azure.md)).
- **Connect it:** the deterministic next step — write `~/.copilot/mcp-config.json` (or import the
  generated Copilot Studio connector) and **restart the client** — never "run `/mcp add`."
- **Next steps (what they unlocked), as concrete prompts:** e.g. *"Ask things like 'total sales by
  region in Superstore last quarter.'"* and *"When you're ready to rebuild these datasources in
  Fabric / Power BI, use the `tableau-migration` skill."*

Prefer **auto-detected facts over questions**: derive the region from the resource group, the connector
host from `mcpEndpoint`, and read secrets from Key Vault by name — don't ask the user for values you
can discover.

## Related skills

- **`tableau-datasource-profiler`** — same Connected App / VDS world; profile or NL-query a
  datasource directly (read-only) without standing up the server. Good for validating the
  Connected App works before/after deploy.
- **`tableau-migration`** — once you can query Tableau live, rebuild its datasources as Fabric /
  Power BI semantic models.
