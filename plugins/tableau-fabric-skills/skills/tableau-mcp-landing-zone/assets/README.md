# Vendored deploy bundle (`assets/`)

This folder is a **self-contained snapshot** of the Tableau MCP landing-zone deploy assets, so the
`tableau-mcp-landing-zone` skill can deploy without cloning anything else.

## Upstream source of truth

These files are **synced from the bridge repo**, which remains the upstream source of truth for the
infrastructure (Bicep, sidecar, CI):

> <https://github.com/Yarbrdab000/Tableau-Fabric-AI-Bridge> → `Play1/deploy/`

| Vendored file | Upstream (bridge repo) |
|---|---|
| `azure/main.bicep` | `Play1/deploy/azure/main.bicep` |
| `azure/azuredeploy.json` | `Play1/deploy/azure/azuredeploy.json` (compiled from the Bicep) |
| `azure/deploy.ps1` | `Play1/deploy/azure/deploy.ps1` |
| `azure/main.parameters.json` | `Play1/deploy/azure/main.parameters.json` (placeholders only) |
| `copilot-studio/mcp-connector.swagger.yaml` + `README.md` | `Play1/deploy/copilot-studio/` |
| `local/docker-compose.yml` + `.env.example` | `Play1/deploy/local/` |

## Not vendored — referenced in the bridge repo

- **Sidecar source + 31 tests** (`Play1/sidecar/`): shipped as the published container image
  `ghcr.io/yarbrdab000/tableau-fabric-ai-bridge-sidecar`. Clone the bridge repo only to develop or
  test the sidecar source.

## One local change vs. upstream

`local/docker-compose.yml` here uses the **published** sidecar image (`image:`) instead of building
from `../../sidecar` (`build:`), so the bundle runs without a source checkout. Everything else is a
verbatim copy.

## Re-syncing

If the bridge repo's `Play1/deploy/` changes, re-copy these files and re-apply the one local edit
above.

> **Never commit secrets** into any file here. `azure/main.parameters.json` is a *shape* template
> only — the Connected App Secret Value and the Sidecar Api Key are supplied at deploy time.
