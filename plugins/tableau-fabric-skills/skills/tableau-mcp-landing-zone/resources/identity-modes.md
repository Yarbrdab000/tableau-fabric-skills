# Workflow: Configure identity (service_account vs passthrough)

Identity is the most consequential choice for this deployment because it decides **what data each
Copilot user can see**. The two modes are mutually exclusive and chosen at deploy time.

| `identityMode` | Each agent user sees | Per-user RLS | Needs |
|---|---|---|---|
| `service_account` (default) | What the one configured Tableau account can see | No | Nothing beyond the Connected App. |
| `passthrough` | Only what *their own* Tableau user may see | **Yes** | Easy Auth/APIM in front + UPN→Tableau mapping + RLS defined on the data. |

> **Fail-closed rule (passthrough):** `ON_UNRESOLVED_IDENTITY=deny` is the only supported value. A
> caller whose UPN can't be mapped — or whose per-user sign-in fails — is **denied**. It never
> silently falls back to the privileged service account. Do not try to "fix" a denied caller by
> switching them to the service account; fix the mapping or the Tableau user instead.

## Step 1 — Create the Tableau Connected App (one time, ~3 min)

This lets the server query Tableau without storing anyone's password.

1. Tableau Cloud → **Settings → Connected Apps**.
2. **New Connected App → Direct Trust**; name it (e.g. `Copilot MCP Bridge`); **Create**.
3. Set it to **Enabled**.
4. Under **Scopes**, enable `tableau:content:read` and `tableau:viz_data_service:read`
   (+ `tableau:insights:read` only if you'll expose Pulse).
5. **Generate New Secret** and copy: **Client ID**, **Secret ID**, **Secret Value** (shown once),
   and your **site content URL** (the slug in the site URL).

> **Store the Connected App in Key Vault (recommended).** Rather than pasting the three values on
> every deploy, keep them in a vault — either as one JSON secret
> `{ "clientId": "...", "secretId": "...", "secretValue": "..." }` or as three secrets with stable
> names (`clientId`, `secretId`, `secretValue`). Hydrate them at deploy time with
> `az keyvault secret show --vault-name <vault> --name <name> --query value -o tsv` (see
> [deploy-azure.md](deploy-azure.md) → "Source secrets from Key Vault"), so no secret material is
> typed into args, chat, or logs. The **Secret Value** is shown only once at generation — capture it
> straight into the vault.

## Step 2 — Choose the service account deliberately

`serviceAccountUsername` is required by the official server at startup and is the identity used for
every call in `service_account` mode.

- Use a **least-privilege Tableau user or group** that can see only the datasources this agent
  should expose.
- A **Site Admin bypasses row-level security** and sees all data — fine for a quick demo, a poor
  production default.

## Passthrough mode — Entra → Tableau per-user RLS (the hero capability)

`passthrough` maps the caller's Entra UPN to a Tableau username, signs a Connected App (Direct
Trust) JWT **as that user**, exchanges it for a Tableau session token, and injects `X-Tableau-Auth`
into the official server — so **Tableau RLS applies per signed-in M365 user**.

Deploy with:

- `identityMode=passthrough`
- `enableEasyAuth=true` (+ `entraClientId`, `entraTenantId`) so the caller's Entra identity actually
  reaches the sidecar
- a UPN → Tableau username mapping (`upnMappingMode`):

| `upnMappingMode` | Behavior |
|---|---|
| `direct` | The UPN **is** the Tableau username (common when both are the email). |
| `transform` | Swap the domain: `upnDomainFrom` → `upnDomainTo`; other domains are denied. |
| `explicit` | Look up an explicit map (`UPN_MAP_JSON` / `UPN_MAP_PATH`, case-insensitive). |

### Passthrough prerequisites — it only enforces something if ALL hold

1. Your datasources **have RLS defined** (user filters with `USERNAME()` / `USERMEMBEROF()`, or
   centralized row-level security).
2. The end users **exist on the Tableau site** — ideally provisioned via **SCIM** from Entra so
   usernames stay aligned (SCIM is not required, it just keeps `direct` matches in sync).
3. The impersonated users are **not Site Admins** (admins bypass RLS).
4. Easy Auth (or APIM) is in front so `X-MS-CLIENT-PRINCIPAL*` is set **authentically** — the
   sidecar only trusts it when `TRUST_EASY_AUTH=true`.

## How the sidecar enforces identity (for reasoning about failures)

- The sidecar **strips** all client-supplied identity headers (`X-Tableau-Auth`,
  `X-MS-CLIENT-PRINCIPAL*`, …) before forwarding, then adds its own.
- Per-user Tableau session tokens are cached **in memory only**, keyed by the full identity tuple
  (server, site, mapped username, tenant, object id, mode, connected-app id) — never by UPN alone —
  and are never logged. A stale-token `401/403` evicts and re-signs-in once.
- Sidecar source (in the bridge repo):
  [`config.py`](https://github.com/Yarbrdab000/Tableau-Fabric-AI-Bridge/blob/main/Play1/sidecar/config.py)
  (startup validation hard-separates the two modes),
  [`identity.py`](https://github.com/Yarbrdab000/Tableau-Fabric-AI-Bridge/blob/main/Play1/sidecar/identity.py)
  (extraction, mapping, JWT, sign-in, cache).

## Decision guide

- **First deploy / demo / no RLS yet** → `service_account` with a least-privilege user.
- **Need each user to see only their rows** → `passthrough` + Easy Auth + a mapping, after
  confirming the four prerequisites above. Otherwise passthrough adds friction without enforcing
  anything.
