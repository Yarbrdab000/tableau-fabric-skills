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

No build step — a "skill" is just a folder the agent reads, so installing one means
putting that folder where your agent looks. Pick the easiest option that works for you.

### Option 1 — Ask your Copilot to install it (easiest, no terminal)

Open **GitHub Copilot CLI** (or Claude Code / Cursor) and paste this, changing nothing:

> Install the agent skills from https://github.com/Yarbrdab000/tableau-fabric-skills .
> Clone it to a temp folder, copy every subfolder of its `skills/` directory into my
> personal skills folder (`~/.copilot/skills/` for Copilot CLI, `~/.claude/skills/` for
> Claude Code), delete the temp clone, then list what you installed.

The agent does the download and copy for you. **Restart the agent** when it finishes and
the skills are live. (That is the whole thing — the options below are the same actions by hand.)

### Option 2 — Copy the folders yourself

You are copying a **folder**. The repo has a `skills/` folder with three subfolders; copy
the one(s) you want into the folder your agent scans for skills.

**Windows (PowerShell)** — installs all three, available in every chat:

```powershell
git clone https://github.com/Yarbrdab000/tableau-fabric-skills.git
Copy-Item .\tableau-fabric-skills\skills\* "$env:USERPROFILE\.copilot\skills\" -Recurse -Force
```

**macOS / Linux (bash):**

```bash
git clone https://github.com/Yarbrdab000/tableau-fabric-skills.git
mkdir -p ~/.copilot/skills
cp -R tableau-fabric-skills/skills/* ~/.copilot/skills/
```

When it finishes you should have, for example, `~/.copilot/skills/tableau-migration/SKILL.md`.
**Restart your agent.** Done. (To install just one skill, copy that single subfolder instead of `*`.)

Destinations by agent and scope:

| Agent | Personal (every chat, any repo) | Project (one repo only) |
|---|---|---|
| GitHub Copilot CLI / VS Code | `~/.copilot/skills/` | `<your-repo>/.github/skills/` |
| Claude Code | `~/.claude/skills/` | `<your-repo>/.claude/skills/` |

> `~` means your home folder. On Windows that is `C:\Users\<you>`, so the personal path is
> `C:\Users\<you>\.copilot\skills\`.

### Option 3 — Plugin marketplace (newer clients only)

```
/plugin marketplace add Yarbrdab000/tableau-fabric-skills
/plugin install tableau-fabric-skills@tableau-collection
```

Installs all three. If `/plugin` is not recognized your client is too old — use Option 1 or 2.

### Check it worked

Ask your agent **"what skills do you have?"** — or just **"profile my Tableau datasource."**
If it knows about the Tableau-to-Fabric skills, you are set.

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