# tableau-mcp-landing-zone (skill)

An agent skill that **deploys and operates Play 1** — the official Tableau MCP server
(`ghcr.io/tableau/tableau-mcp`) behind a Microsoft auth sidecar — so business users can ask
natural-language questions about Tableau data from **Copilot Studio / M365 Copilot / Azure AI
Foundry**.

This skill is the **navigator/operator**, and it **vendors a self-contained deploy bundle** under
[`assets/`](assets/) (Bicep, compiled ARM, `deploy.ps1`, the Copilot Studio connector, a
docker-compose harness) so it deploys without cloning anything else. The bundle is **maintained here**
and is the source of truth for the deploy infra. The auth **sidecar** ships as a prebuilt container
image (`ghcr.io/yarbrdab000/tableau-fabric-ai-bridge-sidecar`), so you never need its source — or any
other repo — to deploy or run this skill.

## What it does

- **Deploy** the landing zone to Azure Container Apps (one-click button or `deploy.ps1`), HTTPS,
  scale-to-zero.
- **Identity:** `service_account` (default) or **Entra → Tableau passthrough** so Tableau
  row-level security applies per signed-in M365 user (fail-closed).
- **Wire** the MCP endpoint into Copilot Studio (custom connector or built-in MCP tool).
- **Evaluate locally** with the docker-compose harness; run the 31 offline sidecar tests.
- **Operate:** Entra Easy Auth hardening, API-key rotation, tool curation, troubleshooting.

## Layout

```
tableau-mcp-landing-zone/
├── SKILL.md                         # primary skill doc: intake checklist, workflow selector, must/prefer/avoid
├── resources/
│   ├── deploy-azure.md              # button + deploy.ps1, params, outputs, verify
│   ├── identity-modes.md            # service_account vs passthrough, Connected App, UPN mapping, RLS reqs
│   ├── copilot-studio-wiring.md     # custom connector + built-in MCP tool, test prompts
│   ├── local-dev.md                 # docker-compose harness + sidecar tests (dev-only; not needed to deploy)
│   └── security-operations.md       # security boundaries, key rotation, Entra hardening, troubleshooting
├── scripts/
│   └── verify_deployment.py         # stdlib fail-loud deploy verifier (health + auth + MCP handshake)
└── assets/                          # self-contained deploy bundle (maintained in this skill)
    ├── azure/                       # main.bicep, azuredeploy.json, deploy.ps1, main.parameters.json
    ├── copilot-studio/              # mcp-connector.swagger.yaml + README
    └── local/                       # docker-compose.yml + .env.example
```

## Start here

> **Heads-up — a skill is not a slash command.** Do not type `/tableau-mcp-landing-zone`
> (or any `/`-command). Just describe what you want in plain language — e.g. *deploy the
> Tableau MCP landing zone to Azure* — and the agent loads this skill automatically.

Open **[SKILL.md](SKILL.md)** → the *Information to collect* checklist tells you exactly what to
gather (and how), then the *Workflow Selector* routes you to the right resource doc. For a deploy,
go straight to [resources/deploy-azure.md](resources/deploy-azure.md).

> **Not this skill:** the no-MCP / Logic-App connectivity route lives in the bridge repo's
> [`Play1_no_MCP/`](https://github.com/Yarbrdab000/Tableau-Fabric-AI-Bridge/tree/main/Play1_no_MCP).
> Migrating Tableau datasources to Fabric semantic models is the separate `tableau-migration` skill
> (also in this repo under `skills/tableau-migration/`).
