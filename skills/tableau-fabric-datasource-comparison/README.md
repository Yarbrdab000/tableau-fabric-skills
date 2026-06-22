# tableau-fabric-datasource-comparison

Read-only **estate comparison** for a Tableau → Microsoft Fabric / Power BI migration. It inventories
every published Tableau datasource and every Fabric semantic model, then ranks each datasource from
**"already in Fabric"** to **"needs rebuild"** so a migration team can size the work: how many
datasources already exist versus how many must be recreated.

See [`SKILL.md`](SKILL.md) for the full agent-facing contract and
[`resources/`](resources/) for the methodology, introspection notes, and report schema.

## Quick start

```powershell
$env:TABLEAU_SERVER    = "https://your-pod.online.tableau.com"
$env:TABLEAU_SITE      = "your-site-content-url"   # "" for the Default site
$env:TABLEAU_PAT_NAME  = "your-pat-name"
$env:TABLEAU_PAT_VALUE = "your-pat-secret"

py -3.11 scripts/compare_estate.py --tableau-live --fabric-live --use-az --format md --out report.md
```

`--use-az` mints the Fabric token with the Azure CLI; alternatively pass `--token` or set
`FABRIC_TOKEN`. Use `--workspaces "A,B"` to scope Fabric, and `--save-tableau-inventory` /
`--save-fabric-inventory` to cache both sides so you can re-score offline with `--weights`.

## What it compares

For every Tableau datasource it scores the best-matching Fabric semantic model on four signals —
**name**, **column overlap**, **type compatibility**, and **physical source** — and assigns a tier
(`Exact / Strong / Partial / Weak / None`). The source signal is connector-agnostic enough to survive a
**Lakehouse intermediary** (Fabric reads a mirror; Tableau connects directly), uses table-name
**containment** so a broad **consolidated** Fabric model that covers a datasource's tables matches at
full strength, and falls back gracefully when the upstream source is **obscured** (composite /
DirectQuery models, referenced datasources).

When Tableau Catalog has not indexed a datasource, the Tableau inventory downloads that datasource's
`.tds` (without its extract) and parses columns + relation tables — including **custom SQL**
(`<relation type='text'>`) FROM/JOIN tables — directly, so cloud-connected datasources still produce a
full schema. On the Fabric side the M parser resolves **Lakehouse / Warehouse / Dataflow / Excel / CSV**
sources and native-SQL queries, not just classic database connectors.

The estate *count* is hardened too: when several datasources claim the **same** Fabric model the report
flags `contested` models, reports the **distinct** models behind "already exists", adds a non-double-
counted **one-to-one assignment** rollup, and lists Fabric models nothing maps to (net-new). Precision
guards down-weight ubiquitous **generic columns** and add a capped **fuzzy name** fallback so neither a
coincidental generic overlap nor a near-miss spelling distorts the verdict; each match carries a one-line
`reason`. See [`resources/comparison-methodology.md`](resources/comparison-methodology.md).

Because a structural matcher is blind to **semantic** equivalence (renamed columns, a renamed asset, a
coincidental overlap of generic column names), every run also emits an **LLM-optional adjudication
queue** — modelled on the `tableau-migration` skill's *second compiler*. It routes the
not-confidently-matched datasources to an agent for a semantic verdict; the deterministic verdict stays
authoritative and the agent's call is folded in only on an explicit `--apply-adjudication` pass. See
[`resources/llm-adjudication.md`](resources/llm-adjudication.md).

A separate **migration-priority** signal then ranks *which* rebuilds matter: each datasource's
downstream usage (attached workbooks + sheets/dashboards, from the Tableau Metadata API with a REST
fallback) fuses with its verdict into `P1 migrate-first … P4 retire candidate` / `Reuse`, so a
datasource with **0–1 attached workbook is deprioritized** even if it needs a full rebuild. See
[`resources/migration-priority.md`](resources/migration-priority.md).

## Layout

```
tableau-fabric-datasource-comparison/
  SKILL.md
  scripts/
    compare.py            # pure, offline scoring engine (the core IP)
    adjudicate.py         # pure LLM-optional "second matcher" router + advisory apply path
    priority.py           # pure migration-priority signal (downstream-usage ranking)
    tableau_inventory.py  # Tableau REST + Metadata API + .tds fallback
    fabric_inventory.py   # Fabric REST + getDefinition / TMDL / M parsing
    compare_estate.py     # CLI orchestrator (live or cached, md/json)
  tests/                  # offline pytest suite (no network)
  resources/
    comparison-methodology.md
    llm-adjudication.md
    migration-priority.md
    empirical-verification.md
    fabric-introspection.md
    tableau-inventory.md
    report-schema.md
```

## Requirements

Python **3.11+**, **standard library only** — no third-party packages. The Fabric token can be supplied
directly or minted via the Azure CLI (`--use-az`).

## Tests

```powershell
cd skills/tableau-fabric-datasource-comparison
py -3.11 -m pytest tests -q
```

The suite is fully offline and fixture-driven; it needs no live Tableau or Fabric access.

## Safety

Read-only on both clouds — the Tableau client always signs out and Fabric uses only read endpoints.
Never commit a downloaded `.tds`/`.tdsx`, a PAT, or a Fabric bearer token.
