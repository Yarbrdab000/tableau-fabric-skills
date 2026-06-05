# tableau-migration-skill

A reusable **Tableau → Microsoft Fabric / Power BI** migration skill, authored to the
[`microsoft/skills-for-fabric`](https://github.com/microsoft/skills-for-fabric) conventions so it can sit
alongside the existing `synapse-migration`, `databricks-migration`, and `hdinsight-migration` skills (which
have no Tableau peer — this fills that gap).

It packages a proven Tableau → Fabric toolkit into an agent-loadable skill. **v1 scope is the semantic-model
path**: rebuild a Tableau published data source as a Power BI semantic model (typed TMDL, inferred
relationships), translate the safe subset of Tableau calculated fields into working DAX (preserving every
original formula), and auto-select a storage mode per datasource so the rebuilt model can point directly at
its original upstream source. Worksheet / dashboard → Power BI report translation is **roadmap (v2)**.

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
| `calc_to_dax.py` | Deterministic Tableau calc → DAX translator (safe subset; `None` on fallback). |
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

## Security

Downloaded Tableau artifacts (`.tds` / `.tdsx` / `.twb` / `.twbx` / `.hyper`) are **sensitive plaintext** and
are git-ignored. Credentials and on-premises gateway setup are a manual security boundary — the skill emits
the model, connection parameters, and bind request, but the user enters credentials.

## License

MIT (see `LICENSE`).
