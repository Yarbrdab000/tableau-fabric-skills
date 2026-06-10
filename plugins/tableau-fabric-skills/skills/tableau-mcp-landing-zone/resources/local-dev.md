# Workflow: Local dev / evaluation

Run the **real** official Tableau MCP image behind the sidecar on your machine — to evaluate the
stack, develop the sidecar, or demo without touching Azure. Also covers the offline sidecar tests
(no Docker needed).

## Full stack with docker-compose (needs Docker)

Harness: [`assets/local/docker-compose.yml`](../assets/local/docker-compose.yml). It runs the official
image internal-only and publishes **only** the sidecar on `:9000` — so the sidecar is the complete
auth boundary, exactly like Azure. The compose file uses the **published** sidecar image, so it runs
without a source checkout.

```bash
cd assets/local               # from the skill folder: skills/tableau-mcp-landing-zone/assets/local
cp .env.example .env          # fill in your Tableau Connected App values
docker compose up             # pulls published images; no source build needed
curl -s localhost:9000/healthz          # expect {"status":"ok", ...}
# MCP over HTTP at  http://localhost:9000/mcp   (header  x-api-key: $SIDECAR_API_KEY)
```

### `.env` highlights ([`assets/local/.env.example`](../assets/local/.env.example))

| Var | Purpose |
|---|---|
| `TABLEAU_MCP_IMAGE` | Official image (pin by digest for real use). |
| `TABLEAU_SERVER` / `TABLEAU_SITE` | Tableau pod + site content URL. |
| `TABLEAU_CONNECTED_APP_CLIENT_ID` / `_SECRET_ID` / `_SECRET_VALUE` | Direct Trust Connected App. |
| `TABLEAU_SERVICE_ACCOUNT_USERNAME` | Tableau user the service account acts as. |
| `SIDECAR_API_KEY` | Shared caller key (send as `x-api-key`). |
| `IDENTITY_MODE` | `service_account` (default) or `passthrough`. |
| `ENABLE_PASSTHROUGH_AUTH` / `TRUST_EASY_AUTH` | Set both `true` for local passthrough testing. |
| `UPN_MAPPING_MODE` + `UPN_DOMAIN_FROM`/`_TO` | Identity mapping (passthrough). |

> **Local passthrough is for testing only.** With `TRUST_EASY_AUTH=true` you send the
> `X-MS-CLIENT-PRINCIPAL-NAME` header yourself — locally that header is **spoofable**. In Azure,
> Container Apps Easy Auth / APIM sets it authentically. Never run a real deployment with a
> spoofable identity source.

> **Never commit a real `.env`.** It holds the Connected App secret.

## Sidecar tests (offline, no Docker)

The sidecar **source and its 31 tests** live in the bridge repo (this skill vendors only the deploy
bundle, not the sidecar source). To run them, clone the bridge repo:

```bash
git clone https://github.com/Yarbrdab000/Tableau-Fabric-AI-Bridge.git
cd Tableau-Fabric-AI-Bridge/Play1/sidecar
python -m venv .venv && . .venv/Scripts/activate    # or bin/activate on *nix
pip install -r requirements.txt pytest
python -m pytest tests -q                           # 31 offline tests
```

They run against an in-process mock upstream — covering caller auth, header stripping, UPN mapping,
JWT/sign-in, and the streaming proxy. Source files under test:
[`proxy.py`](https://github.com/Yarbrdab000/Tableau-Fabric-AI-Bridge/blob/main/Play1/sidecar/proxy.py)
(`/healthz`, `/mcp` GET/POST/DELETE streaming reverse proxy),
[`identity.py`](https://github.com/Yarbrdab000/Tableau-Fabric-AI-Bridge/blob/main/Play1/sidecar/identity.py)
(Entra extraction, UPN mapping, Connected App JWT, per-user sign-in, token cache),
[`config.py`](https://github.com/Yarbrdab000/Tableau-Fabric-AI-Bridge/blob/main/Play1/sidecar/config.py)
(env config + startup validation that hard-separates the two identity modes).

## From local to Azure

Once the local smoke passes, deploy the same images to Azure with
[deploy-azure.md](deploy-azure.md) — the env vars map 1:1 to the Bicep parameters.
