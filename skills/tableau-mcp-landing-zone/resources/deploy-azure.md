# Workflow: Deploy the landing zone to Azure

Stands up the official Tableau MCP server behind the auth sidecar in the user's Azure tenant.
Two paths — the portal **button** (best for non-developers) and the **`deploy.ps1`** CLI (best
when you're driving from a shell). Both produce the same Container App with HTTPS + scale-to-zero.

> **Prerequisite:** an **Enabled** Tableau Connected App (Direct Trust) with `tableau:content:read`
> + `tableau:viz_data_service:read`. See [identity-modes.md](identity-modes.md) Step 1. You need its
> **Client ID**, **Secret ID**, **Secret Value**, plus the Tableau **pod URL** and **site slug**.

> **Secret discipline.** The Connected App **Secret Value** and the **Sidecar Api Key** are secrets.
> Pass them at deploy time (portal form or CLI args) or via a **git-ignored** local parameters file —
> never commit them, never echo them into chat/logs, and never write them into the repo's
> `assets/azure/main.parameters.json`. The Bicep marks both `@secure()`, so they stay out of Azure
> deployment history; the only remaining leak vector is your shell/agent, so keep them out of it.
> **Never ask the user to paste a secret into chat, and never paste one yourself** — read it
> from Key Vault / an env var (below). If a secret ever appears in the transcript it is compromised;
> have the user rotate it (new Connected App secret / new api key) before deploying.

### Source secrets from Key Vault (recommended, especially for agents)

To keep secret material out of args, chat, and logs, read each secret from an existing Key Vault
into a local variable at deploy time instead of pasting it:

```powershell
$secret = az keyvault secret show --vault-name <your-vault> --name <connected-app-secret-name> --query value -o tsv
./deploy.ps1 -ResourceGroup my-rg ... -ConnectedAppSecretValue $secret
```

- The value lives only in a local variable for the call — never echo it.
- **The Sidecar Api Key is never printed.** `deploy.ps1` does not echo the key — if it generates one,
  it tells you to read it back from the `sidecar-api-key` Container App secret (or your Key Vault when
  `-UseKeyVault`), so the value never lands in a transcript:

  ```powershell
  az containerapp secret show -n <container-app> -g <resource-group> --secret-name sidecar-api-key --query value -o tsv
  ```

  Prefer pre-generating the key and storing it in Key Vault, then passing it via the same
  `az keyvault secret show` pattern; hand only a Key Vault *reference* downstream, not the raw value.
- A pasted browser URL like `https://10ay.online.tableau.com/#/site/<slug>/...` is **not** the
  `-TableauServer` value: pass the pod URL only (`https://10ay.online.tableau.com`) and the
  `<slug>` segment as `-TableauSite` (an empty slug means the **Default** site).

## Option A — Deploy to Azure button (portal form, no CLI)

[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2FYarbrdab000%2Ftableau-fabric-skills%2Fmain%2Fskills%2Ftableau-mcp-landing-zone%2Fassets%2Fazure%2Fazuredeploy.json)

1. Click the button above (it loads the vendored
   [`assets/azure/azuredeploy.json`](../assets/azure/azuredeploy.json) into the Azure portal).
2. In the portal form choose **Subscription**, **Resource group**, **Region**, then fill:

   | Field | Value |
   |---|---|
   | **Tableau Server** | pod URL, e.g. `https://10ay.online.tableau.com` |
   | **Tableau Site** | site content URL (slug) |
   | **Connected App Client Id / Secret Id / Secret Value** | from the Connected App |
   | **Service Account Username** | the least-privilege Tableau user the agent acts as |
   | **Identity Mode** | `service_account` (default) or `passthrough` (per-user RLS) |
   | **Sidecar Api Key** | a long random string (e.g. a GUID) — paste into Copilot later |

3. **Review + create → Create**, wait for completion.
4. Open the deployment's **Outputs** and copy **`mcpEndpoint`** (looks like
   `https://tableau-mcp.<region>.azurecontainerapps.io/mcp`).

## Option B — `deploy.ps1` (Azure CLI)

Requires `az login` first. The OFFICIAL image is pulled from GHCR; the sidecar image is the
vendor-published one referenced in the script's `-SidecarImage` default.

Service-account mode, api-key auth (the common case). **Read the secret from Key Vault into a
variable first — never type the literal secret value into the command:**

```powershell
cd assets/azure            # from the skill folder: skills/tableau-mcp-landing-zone/assets/azure
$secret = az keyvault secret show --vault-name <your-vault> --name tableau-ca-secret-value --query value -o tsv
./deploy.ps1 -ResourceGroup my-rg `
             -TableauServer https://10ay.online.tableau.com `
             -TableauSite my-site `
             -ConnectedAppClientId <id> -ConnectedAppSecretId <id> `
             -ConnectedAppSecretValue $secret `
             -ServiceAccountUsername svc-mcp@company.com `
             -SidecarApiKey (New-Guid).Guid
```

Per-user RLS (passthrough behind Easy Auth) — see [identity-modes.md](identity-modes.md):

```powershell
$secret = az keyvault secret show --vault-name <your-vault> --name tableau-ca-secret-value --query value -o tsv
./deploy.ps1 -ResourceGroup my-rg `
             -TableauServer https://10ay.online.tableau.com -TableauSite my-site `
             -ConnectedAppClientId <id> -ConnectedAppSecretId <id> -ConnectedAppSecretValue $secret `
             -ServiceAccountUsername svc-mcp@company.com `
             -IdentityMode passthrough -EnableEasyAuth `
             -EntraTenantId <tenant-guid> -EntraClientId <app-client-id>
```

If `-SidecarApiKey` is omitted while api-key auth is on, the script **generates a random one but does
not print it** — read it back from the `sidecar-api-key` Container App secret (command above). On
success the script prints `mcpEndpoint` and `healthUrl`, the curated tool set (with a Pulse hint), and
writes a host-filled `mcp-connector.generated.swagger.yaml` for Copilot Studio.

> **Region is auto-derived.** `-Location` defaults to the **resource group's** region (the deployment
> targets that RG), so resources never land cross-region. The script creates the RG in `eastus` only
> if it doesn't exist and no `-Location` is given; an explicit `-Location` that differs from an
> existing RG is overridden (with a warning) to the RG's region.

## Outputs to capture

| Output | Use |
|---|---|
| `mcpEndpoint` | Register this in Copilot Studio ([copilot-studio-wiring.md](copilot-studio-wiring.md)). |
| `healthUrl` | Smoke check — open in a browser, expect `{"status":"ok"}`. |
| `identityModeOut` | Confirms the deployed identity mode. |
| `easyAuthEnabled` | Confirms whether the Entra front door is on. |

## Verify

1. Open `healthUrl` → expect `{"status":"ok"}` (first hit after idle may cold-start ~15s).
2. Proceed to [copilot-studio-wiring.md](copilot-studio-wiring.md) and run one NL query.
3. (Optional) Run the bundled [`scripts/verify_deployment.py`](../scripts/verify_deployment.py) — a
   stdlib, fail-loud probe that sends a real `initialize` + `tools/list` and asserts the identity
   mode and a minimum tool count. It reads the api key from `$env:SIDECAR_API_KEY` (never an arg);
   `--base-url` is non-secret. Expect a brief cold-start on the first call. It also prints the enabled
   tool set and whether Pulse is on.
4. (Optional) Smoke-query live data with the stdlib [`scripts/query.py`](../scripts/query.py):
   `py -3.11 scripts/query.py --base-url <mcpEndpoint> list` (reads `$env:SIDECAR_API_KEY`). Use it
   instead of PowerShell `Invoke-WebRequest`, which hangs on the SSE stream. It refuses to guess a
   datasource — run `list` first, then pass `--datasource <luid>` to `metadata` / `query`.

## Cost

The Container App **scales to zero** when idle (`minReplicas=0`), so occasional use is typically a
few dollars/month or less.

## Tear down

A test or demo deployment still bills for storage, the Log Analytics workspace, and any non-idle
replicas until you remove it. When you're done verifying (including through Microsoft 365 Copilot),
delete the deployment. If you deployed into a **dedicated** resource group, the simplest cleanup is:

```powershell
az group delete --name <your-resource-group> --yes --no-wait
```

That removes the Container App, its managed environment, the Log Analytics workspace, and (if used)
the Key Vault created by the deploy. If the resource group also holds resources you want to keep,
delete just the Container App and its `*-env` managed environment instead.

## Common parameters (beyond the form)

Override these via `deploy.ps1` params or the Bicep ([`assets/azure/main.bicep`](../assets/azure/main.bicep)):
`includeTools` / `maxResultLimits` (tool curation), `useKeyVault` (secrets in Key Vault via managed
identity), `minReplicas` / `maxReplicas`, `tableauMcpImage` / `sidecarImage`.

> **Pin images by digest for production.** The defaults use readable tags (`tableauMcpImage`
> `…:2.7.4`, `sidecarImage` `…:latest`). For deploys that must be byte-reproducible and immune to a
> retag, override with `…@sha256:<digest>`. The verified `2.7.4` digest is
> `sha256:10a043fea52c6152ab1d86222540aa1bc2ba021411dc772bc3f48a3c36b54de1`; resolve others with
> `docker buildx imagetools inspect <image:tag>`. **Version note:** the upstream HTTP path
> (`/tableau-mcp`) and the `ENABLE_MCP_SITE_SETTINGS=false` default are coupled to the 2.7.x line —
> re-verify both if you move to a different tag/digest.

## Tool governance: site-driven overrides (opt-in)

The deploy sets `ENABLE_MCP_SITE_SETTINGS=false`. The official server otherwise runs a startup
site-settings probe that needs the `tableau:mcp_site_settings:read` scope, which a Direct-Trust
Connected App typically does not grant — leaving it on **500s the `initialize` handshake**. The
curated tool set still registers with it off. To instead let Tableau **site settings** govern which
tools are enabled, grant the Connected App `tableau:mcp_site_settings:read` and set
`ENABLE_MCP_SITE_SETTINGS=true` (requires Tableau REST API ≥ 3.29 and the feature live on your site).
