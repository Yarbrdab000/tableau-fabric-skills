# CLAUDE.md

Instructions for Claude Code working in this repository. The complete agent guide is
[`AGENTS.md`](AGENTS.md); the non-negotiable rules are repeated here.

## Install / consume (for agents)

To make these skills actually load in a client, register the **plugin** — do **not** copy folders
into `~/.claude/skills/` or `~/.copilot/skills/` expecting an auto-scan:

```
/plugin marketplace add Yarbrdab000/tableau-fabric-skills
/plugin install tableau-fabric-skills@tableau-collection
```

Start a new session, then verify with `/plugin list` and `/skills list`. Full details /
uninstall: [`INSTALL.md`](INSTALL.md) / [`UNINSTALL.md`](UNINSTALL.md).

## Project

`tableau-fabric-skills` — a standalone collection of three Tableau → Microsoft Fabric / Power BI
agent skills (`tableau-datasource-profiler`, `tableau-mcp-landing-zone`, `tableau-migration`).
Canonical sources live in `skills/<name>/`; byte-identical mirror copies live in
`plugins/tableau-fabric-skills/skills/<name>/`.

## Must-follow rules

- **Environment:** Windows PowerShell — no PowerShell 7-only syntax (`&&`, `||`, `??`, `?.`) and
  no heredocs (write a temp file). Use `py -3.11` (a bare `py` lacks pytest). Read BOM files as
  `utf-8-sig`; write JSON manifests as UTF-8 without a BOM.
- **Tests:** `cd skills\tableau-migration; py -3.11 -m pytest tests -q` must stay green
  (baseline 956 passed / 1 skipped / 1 xfailed). Report-schema changes are **additive only** —
  never rename or remove existing keys.
- **Fractal mirror:** after editing any `skills/<name>/` file, re-mirror it into
  `plugins/tableau-fabric-skills/skills/<name>/` with `robocopy /MIR` (excluding `__pycache__`,
  `.pytest_cache`, `*.pyc`) before committing.
- **Secrets:** never commit a real `.env`, a Tableau workbook/extract, a PAT, a Connected App
  secret, or a sidecar key. Only `.env.example` templates. See [`SECURITY.md`](SECURITY.md) and
  [`.gitleaks.toml`](.gitleaks.toml).
- **Clean room:** `cyphou/Tableau-To-PowerBI` is **reference-only — copy nothing** (its MIT
  license notwithstanding); `microsoft/skills-for-fabric` is a **structure/convention model**
  used with our own prose. Details in [`CLEANROOM.md`](CLEANROOM.md) and [`AGENTS.md`](AGENTS.md).
- **Commits:** the user is the author; append
  `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`. Do not push unless
  asked.

## MCP

To exercise the landing zone from Claude Code, wire the deployed (or local) endpoint per
[`skills/tableau-mcp-landing-zone/resources/mcp-clients.md`](skills/tableau-mcp-landing-zone/resources/mcp-clients.md)
— either `claude mcp add --transport http tableau <endpoint> --header "x-api-key: <key>"` or an
`.mcp.json` entry with `type: "http"`.
