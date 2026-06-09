# tableau-migration-skill

A reusable **Tableau → Microsoft Fabric / Power BI** migration skill, authored to the
[`microsoft/skills-for-fabric`](https://github.com/microsoft/skills-for-fabric) conventions so it can sit
alongside the existing `synapse-migration`, `databricks-migration`, and `hdinsight-migration` skills (which
have no Tableau peer — this fills that gap).

It packages a proven Tableau → Fabric toolkit into an agent-loadable skill. **v1 scope is the semantic-model
path**: rebuild a Tableau published data source as a Power BI semantic model (typed TMDL, inferred
relationships), translate the safe subset of Tableau calculated fields into working DAX (preserving every
original formula), and auto-select a storage mode per datasource so the rebuilt model can point directly at
its original upstream source (or falls back to land-to-Delta + DirectLake when a direct rebuild is not safe).
Worksheet / dashboard → Power BI report translation is **roadmap (v2)**.

## Install

This is an **agent skill**: install it into a *code-executing* Copilot (GitHub Copilot CLI, VS Code Copilot,
Claude Code, Cursor, …). The agent reads `SKILL.md`, and its `description` triggers (e.g. *"migrate from
tableau"*, *"tableau to fabric"*, *"convert tableau calculation to dax"*) fire automatically — so once it's
installed you just describe the migration and the Copilot drives it (including a self-contained Fabric deploy).

> M365 / Office Copilot is **not** a target — it can't execute the scripts. Use a coding-agent Copilot.

**Install = drop the skill folder where your agent discovers skills.** Agent skills are loaded from
well-known directories — there is no build step. Clone the repo, then copy `skills/tableau-migration/`
to one of these locations:

```bash
git clone https://github.com/Yarbrdab000/tableau-migration-skill.git
```

| Agent | Copy `skills/tableau-migration/` to | Scope |
|---|---|---|
| **GitHub Copilot CLI — personal (recommended)** | `~/.copilot/skills/tableau-migration/` | Every chat, any repo — true plug-and-play |
| GitHub Copilot CLI / VS Code — project | your repo's `.github/skills/tableau-migration/` | That repo only (shared with the team) |
| Claude Code | `~/.claude/skills/` (personal) or `.claude/skills/` (project) | Personal or project |
| Cursor / Windsurf / Codex | anywhere in the repo, then point the agent at `skills/tableau-migration/SKILL.md` | Project |

The **personal** path (`~/.copilot/skills/`) is the smoothest: install once and the skill is available
in every new Copilot CLI chat regardless of which repo you're in. See GitHub's docs:
[Adding agent skills for GitHub Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-skills).

Then start a chat and say e.g. *"migrate my Tableau Superstore datasource to a Fabric semantic model."*
The agent will ask whether your datasource is a **local file** (`.tds`/`.tdsx`/`.twb`/`.twbx`) or a
**live published datasource** (pulled via Tableau's REST/Metadata API) and drive the rest.
The only Python requirement is **3.11+** (the scripts are stdlib-only — no pip install needed to run them).

> **Plugin marketplace (`/plugin marketplace add …`)** is a separate, newer Copilot CLI / Claude Code
> packaging mechanism and is **not available in every build** — if `/plugin` does nothing in your client,
> use the folder copy above, which always works. A marketplace manifest is included in the repo for
> clients that do support it.

### Updating an installed copy

The skill ships a version stamp at `skills/tableau-migration/VERSION` and a self-update runbook the
agent can execute. To upgrade, just tell your Copilot **"check for updates / update the tableau-migration
skill"** — it reads the installed `VERSION`, compares it against this repo, and (if newer) reinstalls by
overwriting `scripts/` + `resources/` + `SKILL.md` wholesale, verifies the result (asserts key functions
exist + runs the tests), and reports the version delta (`1.0.0 → 1.1.0`). Full procedure:
[`skills/tableau-migration/resources/self-update.md`](skills/tableau-migration/resources/self-update.md).
Because skills load at session start, start a **new** chat for the update to take effect. If your client
supports `gh skill`, `gh skill update tableau-migration` is an equivalent managed path.

## Layout

```
skills/tableau-migration/
  SKILL.md            # the skill (full skills-for-fabric authoring contract)
  resources/          # on-demand .md docs, loaded per migration phase
  scripts/            # pure-Python, stdlib-only, offline-tested cores
  tests/              # pytest suite (offline assertions)
```

The repo mirrors the upstream `skills/<name>/` layout so the `tableau-migration` folder is portable into
`microsoft/skills-for-fabric` later (via a fork + CLA).

## Scripts

All scripts are deterministic, offline, and stdlib-only (no Spark / pandas required to run them):

| Script | Purpose |
|---|---|
| `calc_to_dax.py` | Deterministic, typed Tableau calc → DAX translator. Recursive-descent parser covering single-field aggregations, arithmetic, `IF`/`ELSEIF`/`IIF`, comparison + `AND`/`OR`/`NOT`, and `ZN`/`IFNULL`/`ISNULL`; returns `None` (→ stub) on anything outside the safe, type-checked subset. |
| `tmdl_generate.py` | TMDL generators: typed columns, tables, measures, relationship inference. |
| `field_resolver.py` | Caption → column resolver for the DirectLake (landed-Delta) path. |
| `storage_mode.py` | Per-datasource storage-mode auto-selection (pure policy). |
| `connection_to_m.py` | Parse Tableau `.tds` → descriptor; emit M partitions + bind details. |
| `assemble_model.py` | Orchestrator: `.tds` → full Fabric SemanticModel definition (TMDL parts + `.platform` + `.pbism`). |
| `deploy_to_fabric.py` | Self-contained Fabric REST deploy (stdlib-only): createOrUpdate / updateDefinition, 202 LRO polling, optional refresh + gateway bind — finishes the migration **in Fabric** without a peer-skill dependency. |

## Tests

```bash
cd skills/tableau-migration
python -m pytest tests -q
```

## Provenance

Distilled from the [`Yarbrdab000/Tableau-Fabric-AI-Bridge`](https://github.com/Yarbrdab000/Tableau-Fabric-AI-Bridge)
6-play toolkit (Play 4 semantic-model generator + calc→DAX translator). This repo is additive packaging — it
does not modify the bridge repo's notebooks.

### Prior art

The breadth of the Tableau → DAX / Power Query mapping space was informed by surveying the MIT-licensed
[`cyphou/Tableau-To-PowerBI`](https://github.com/cyphou/Tableau-To-PowerBI) project, which gave a useful
reference for which Tableau constructs have clean Power BI equivalents. **No third-party source code is
vendored in this repository** — the engine here is an independent recursive-descent implementation, and only
the (non-copyrightable) language-to-language equivalences were used. See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

## Security

Downloaded Tableau artifacts (`.tds` / `.tdsx` / `.twb` / `.twbx` / `.hyper`) are **sensitive plaintext** and
are git-ignored. Credentials and on-premises gateway setup are a manual security boundary — the skill emits
the model, connection parameters, and the structured bind inputs, but the user enters credentials.

## License

MIT (see `LICENSE`).
