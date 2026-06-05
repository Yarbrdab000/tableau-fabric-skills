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
