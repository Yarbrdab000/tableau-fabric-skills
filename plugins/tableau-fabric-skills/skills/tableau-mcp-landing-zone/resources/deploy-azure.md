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

Service-account mode, api-key auth (the common case):

```powershell
cd assets/azure            # from the skill folder: skills/tableau-mcp-landing-zone/assets/azure
./deploy.ps1 -ResourceGroup my-rg `
             -TableauServer https://10ay.online.tableau.com `
             -TableauSite my-site `
             -ConnectedAppClientId <id> -ConnectedAppSecretId <id> `
             -ConnectedAppSecretValue <value> `
             -ServiceAccountUsername svc-mcp@company.com `
             -SidecarApiKey (New-Guid).Guid
```

Per-user RLS (passthrough behind Easy Auth) — see [identity-modes.md](identity-modes.md):

```powershell
./deploy.ps1 -ResourceGroup my-rg `
             -TableauServer https://10ay.online.tableau.com -TableauSite my-site `
             -ConnectedAppClientId <id> -ConnectedAppSecretId <id> -ConnectedAppSecretValue <value> `
             -ServiceAccountUsername svc-mcp@company.com `
             -IdentityMode passthrough -EnableEasyAuth `
             -EntraTenantId <tenant-guid> -EntraClientId <app-client-id>
```

If `-SidecarApiKey` is omitted while api-key auth is on, the script **generates one and prints it** —
capture it. On success the script prints `mcpEndpoint`, the `x-api-key` to send, and `healthUrl`.

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

## Cost

The Container App **scales to zero** when idle (`minReplicas=0`), so occasional use is typically a
few dollars/month or less.

## Common parameters (beyond the form)

Override these via `deploy.ps1` params or the Bicep ([`assets/azure/main.bicep`](../assets/azure/main.bicep)):
`includeTools` / `maxResultLimits` (tool curation), `useKeyVault` (secrets in Key Vault via managed
identity), `minReplicas` / `maxReplicas`, `tableauMcpImage` / `sidecarImage`.

> **Pin images by digest for production.** Replace the tag defaults of `tableauMcpImage` and
> `sidecarImage` with `…@sha256:<digest>` so deploys are reproducible and can't silently pull a
> changed image. Resolve a digest with `docker buildx imagetools inspect <image:tag>`.
