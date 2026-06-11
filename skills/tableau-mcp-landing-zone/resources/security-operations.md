# Workflow: Secure & operate

Hardening, key rotation, tool curation, and troubleshooting for a deployed landing zone. Read this
before exposing the endpoint beyond a demo.

## Security boundaries (what protects what)

- **Caller auth:** `x-api-key` (a shared secret; treat like a password) and/or **Microsoft Entra
  Easy Auth**. With api-key enabled, Easy Auth runs in `AllowAnonymous` and the sidecar enforces the
  key; with `allowApiKey=false`, Easy Auth returns `401` at the platform edge for unauthenticated
  callers.
- **No public official server:** ingress targets only the sidecar; the official container is
  unreachable from the internet, which is why `DANGEROUSLY_DISABLE_OAUTH=true` on it is safe.
- **Header spoofing:** the sidecar strips all client-supplied identity headers (`X-Tableau-Auth`,
  `X-MS-CLIENT-PRINCIPAL*`, …) before adding its own; it trusts Easy Auth's principal header only
  when `TRUST_EASY_AUTH=true` (i.e. behind a real gateway).
- **Per-user RLS (passthrough):** fail-closed on unresolved identity; per-user Tableau session
  tokens are cached in memory only, keyed by the full identity tuple, and never logged.
- **Secrets:** plain Container App secrets by default; opt into **Key Vault + managed identity**
  with `useKeyVault=true`.
- **Least privilege:** scope the Connected App to `tableau:content:read` +
  `tableau:viz_data_service:read` (plus the Pulse insight scopes only for Pulse — see *Scopes by
  capability* in [identity-modes.md](identity-modes.md)). In `service_account`
  mode use a least-privilege Tableau user — a Site Admin bypasses RLS.

## Harden with Microsoft Entra (recommended for production)

The API key already restricts callers. For an identity-based layer, turn on the **Entra "Easy Auth"
front door**:

- **At deploy time:** `enableEasyAuth=true` + `entraClientId` + `entraTenantId` (pre-create an Entra
  app registration for the endpoint). With api-key still on, Easy Auth is `AllowAnonymous` and the
  sidecar enforces the key; set `allowApiKey=false` to require Entra sign-in for **every** call.
- **In the portal:** Container App → **Authentication → Add identity provider → Microsoft →
  Require authentication**, then point the Copilot Studio connector at **OAuth 2.0 (Microsoft
  Entra)** instead of the API key.

## Rotate the API key

1. Update the `sidecar-api-key` Container App secret (new long random value).
2. Update the Copilot Studio connector connection to send the new key.
3. The old key stops working as soon as the revision picks up the new secret.

Rotate immediately if a key was ever pasted into chat, a ticket, or a commit.

## Rotate the Connected App secret

The Connected App **Secret Value** signs the Tableau JWT — treat it like the api key.

1. Tableau -> **Settings -> Connected Apps -> Direct Trust** -> open the app -> **Generate New
   Secret**, then delete the old secret.
2. Store the new value in Key Vault (or your secret store) and update the `connected-app-secret`
   Container App secret (or redeploy) so the sidecar signs with the new value.
3. The old secret stops working the moment it is deleted in Tableau.

**Rotate immediately if the secret value or the api key ever appeared in chat, a ticket, a log, or
a commit** — a secret that entered a transcript is compromised even if later deleted.

## Curate tools

The landing zone ships `includeTools=datasource,content-exploration,workbook,view,pulse` and
`maxResultLimits=query-datasource:100` — the official server's full NL-analytics set (data, content, workbooks, views, Pulse), with the
default row cap. Adjust at deploy time:

- Slim the set (e.g. drop `pulse` or `view`), or add the remaining groups (`project`, `token-management`), by editing `includeTools`.
- Raise/lower row caps via `maxResultLimits`.
- Fewer, well-described tools orchestrate more reliably on weaker models.

> **Scopes follow tools.** Each group needs matching Connected App scopes — e.g. the `view` group
> needs `tableau:views:download`, and the `pulse` group needs the Pulse insight scope family
> (`tableau:insight_definitions_metrics:read`, `tableau:insight_metrics:read`,
> `tableau:metric_subscriptions:read`, `tableau:insights:read`, `tableau:insight_brief:create`).
> Enabling a tool without its scope yields a **401/403 at call time**, not a deploy error. Full map:
> the *Scopes by capability* table in [identity-modes.md](identity-modes.md).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `healthUrl` doesn't respond immediately | Scale-to-zero cold start — retry after ~15s. |
| Copilot can't connect | Re-check the `x-api-key` (or `Authorization: Bearer <key>`) matches the deployed `sidecarApiKey`; confirm generative orchestration is ON. |
| `VizQL Data Service is not available` | The site needs Tableau **2025.1+ with VDS enabled**. |
| `VDS rate limit hit (429)` | VDS allows ~100 calls/hour per Creator; retry shortly. |
| Empty/partial query results | `service_account`: the account's RLS may limit rows — check `serviceAccountUsername` can see the data (without over-privileging). `passthrough`: the signed-in user may legitimately have no rows. |
| Sign-in fails | Verify the Connected App is **Enabled** and the secret value was copied correctly. |
| `403`/denied in passthrough | The caller's UPN didn't map to a Tableau user — check `upnMappingMode` and that the user exists on the site. |

## For vendors — publish the sidecar image once

Customers click *Deploy to Azure*, which pulls two public images: the **official**
`ghcr.io/tableau/tableau-mcp` (already public) and the **sidecar**. To publish the sidecar:

1. Merge to `main`; the Action `.github/workflows/build-sidecar-image.yml` builds + pushes
   `ghcr.io/<owner>/tableau-fabric-ai-bridge-sidecar:latest` (no local Docker needed).
2. In the repo's **Packages**, set the image visibility to **Public** so the deploy button can pull
   it without registry credentials.
3. Share the **Deploy to Azure** button/link.

> Pin the **official** image by digest (`tableauMcpImage` / `main.parameters.json`) for reproducible
> production deploys.
