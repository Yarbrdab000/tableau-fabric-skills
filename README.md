# tableau-fabric-skills

A collection of **agent skills** for moving from **Tableau** to **Microsoft Fabric / Power BI**,
authored to the [`microsoft/skills-for-fabric`](https://github.com/microsoft/skills-for-fabric)
conventions (one `skills/` folder, one marketplace manifest) so they install into code-executing
Copilots — **GitHub Copilot CLI**, VS Code Copilot, Claude Code, Cursor.

> These are the **agent-driven** counterpart to the do-it-yourself notebooks in the
> [`Tableau-Fabric-AI-Bridge`](https://github.com/Yarbrdab000/Tableau-Fabric-AI-Bridge) repo. The
> bridge is the manual toolkit; this repo packages the same capabilities as skills a Copilot drives.

## The skills

| Skill | What it does | Use when |
|---|---|---|
| **[`tableau-datasource-profiler`](skills/tableau-datasource-profiler/)** | Read-only profile of a published Tableau datasource (fields, types, calc formulas, lineage, migration signals) and natural-language querying via the VizQL Data Service. | You want to inventory, audit, or query a datasource — or validate a Connected App — before migrating. |
| **[`tableau-mcp-landing-zone`](skills/tableau-mcp-landing-zone/)** | Deploy the **official** Tableau MCP server behind a Microsoft auth sidecar to Azure and wire it into Copilot Studio, so business users ask natural-language questions about Tableau data. Optional Entra to Tableau per-user RLS. | You want live, governed natural-language access to Tableau from Microsoft Copilot. |
| **[`tableau-migration`](skills/tableau-migration/)** | Rebuild Tableau datasources as Power BI semantic models: typed TMDL, inferred relationships, deterministic calc to DAX (every formula preserved), storage-mode auto-selection, self-contained Fabric REST deploy. | You want to migrate a datasource into a Fabric / Power BI semantic model. |

They share one Tableau Connected App and compose naturally: **profile** to validate, **serve** live
over MCP, **migrate** into Fabric.

## Install

These are agent skills — **no build step**. Either copy a skill folder where your agent discovers
skills, or (on clients that support it) add the plugin marketplace.

### Folder copy (always works)

```bash
git clone https://github.com/Yarbrdab000/tableau-fabric-skills.git
```

Then copy whichever skill folder(s) you want:

| Agent | Copy `skills/<name>/` to | Scope |
|---|---|---|
| **GitHub Copilot CLI — personal (recommended)** | `~/.copilot/skills/<name>/` | Every chat, any repo |
| GitHub Copilot CLI / VS Code — project | your repo's `.github/skills/<name>/` | That repo only |
| Claude Code | `~/.claude/skills/` (personal) or `.claude/skills/` (project) | Personal or project |

### Plugin marketplace (clients that support `/plugin`)

```
/plugin marketplace add Yarbrdab000/tableau-fabric-skills
/plugin install tableau-fabric-skills@tableau-collection
```

Installs all three skills. (`/plugin` is newer and not in every build — use the folder copy if it is
unavailable.)

## Layout

```
skills/                              # canonical skills (source of truth)
  tableau-datasource-profiler/
  tableau-mcp-landing-zone/          # includes a vendored assets/ deploy bundle
  tableau-migration/
plugins/
  tableau-fabric-skills/             # self-contained bundle plugin (mirrors skills/)
.claude-plugin/marketplace.json      # marketplace manifest (+ .github/plugin/marketplace.json)
```

## Requirements

Python **3.11+**. `tableau-migration` is standard-library only; `tableau-datasource-profiler` needs
`requests` (`pip install -r skills/tableau-datasource-profiler/requirements.txt`);
`tableau-mcp-landing-zone` deploys with the Azure CLI / Docker.

## Provenance & license

Distilled from the [`Tableau-Fabric-AI-Bridge`](https://github.com/Yarbrdab000/Tableau-Fabric-AI-Bridge)
6-play toolkit. The `tableau-mcp-landing-zone` skill **wraps the official, unmodified**
`ghcr.io/tableau/tableau-mcp` image (Apache-2.0). See [`CLEANROOM.md`](CLEANROOM.md) and
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md). MIT licensed (see [`LICENSE`](LICENSE)).