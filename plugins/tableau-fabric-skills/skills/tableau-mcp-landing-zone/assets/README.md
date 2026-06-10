# Vendored deploy bundle (`assets/`)

This folder is a **self-contained snapshot** of the Tableau MCP landing-zone deploy assets, so the
`tableau-mcp-landing-zone` skill can deploy without cloning anything else.

## Source of truth

These files are **maintained here, in this skill** — this `assets/` bundle is the source of truth for
the deploy infrastructure. Nothing is synced from another repository; edit these files directly.

| File | Purpose |
|---|---|
| `azure/main.bicep` | Container App + sidecar + official image, identity wiring, optional Key Vault / Easy Auth. |
| `azure/azuredeploy.json` | Portal template compiled from the Bicep (backs the Deploy-to-Azure button). |
| `azure/deploy.ps1` | `az`-based CLI deploy (prints `mcpEndpoint` + `healthUrl`). |
| `azure/main.parameters.json` | Parameter shape (placeholders only). |
| `copilot-studio/mcp-connector.swagger.yaml` + `README.md` | Custom-connector swagger + wiring guide. |
| `local/docker-compose.yml` + `.env.example` | Local evaluation harness. |

## Not vendored (ships as a prebuilt image)

- **Sidecar source + 31 tests** (`Play1/sidecar/`): shipped as the published container image
  `ghcr.io/yarbrdab000/tableau-fabric-ai-bridge-sidecar`. Clone the bridge repo only to develop or
  test the sidecar source.

## Local harness

`local/docker-compose.yml` uses the **published** sidecar image (`image:`) rather than building from
source, so the bundle runs without a separate source checkout.

> **Never commit secrets** into any file here. `azure/main.parameters.json` is a *shape* template
> only — the Connected App Secret Value and the Sidecar Api Key are supplied at deploy time.
