# Public API + end-to-end snippet

Every bundled script is **stdlib-only and importable**. This is the copy-paste reference so an
agent never has to grep `^def ` to learn signatures. The whole migration is three calls:
**download → migrate → deploy**. None of this touches credentials except where noted (the user
owns those).

```python
import sys; sys.path.insert(0, "scripts")   # or run from skills/tableau-migration/
```

---

## 1. Download from Tableau — `fetch_tds.py`

REST sign-in (PAT **or** Connected-App JWT), find a published datasource by name, download it, and
unzip the inner `.tds` from a `.tdsx`. Stdlib-only; works against Tableau Cloud and Server.

```python
from fetch_tds import sign_in, resolve_datasource_luid, download_datasource, is_zip, inner_tds_from_zip

server, ver, site = "https://10ay.online.tableau.com", "3.24", "<site-content-url>"
token, site_id = sign_in(server, ver, site, pat_name="<PAT name>", pat_secret="<PAT secret>")
luid          = resolve_datasource_luid(server, ver, site_id, token, "Snowflake-Superstore")
fname, raw, _ = download_datasource(server, ver, site_id, token, luid)   # includeExtract=False
tds_text      = inner_tds_from_zip(raw) if is_zip(raw) else raw.decode("utf-8-sig")
```

CLI equivalent: `py scripts\fetch_tds.py --server ... --site ... --datasource-name "..." --auth pat`.
Credentials come from `--pat-name/--pat-secret` or env vars; the script never logs the secret.
(The companion **tableau-datasource-profiler** skill can pull field-level stats first if you want to
size or scope the migration.)

---

## 2. Migrate — `assemble_model.py`

### One call: `migrate_datasource`

```python
from assemble_model import migrate_datasource

out = migrate_datasource(
    tds_text,                 # a .tdsx/.tds PATH, raw bytes, or .tds XML text — all accepted
    model_name="Snowflake-Superstore",
    write_to=r"C:\out",       # optional: also persist to disk
    as_pbip=True,             # optional: write an openable .pbip (else a .SemanticModel folder)
)
# out = {"parts": {path: tmdl}, "report": {...}, "bind": {...connection target...},
#        "pbip": r"C:\out\Snowflake-Superstore.pbip"}      # or "model_dir" when as_pbip=False
```

- **Calculated fields are auto-extracted** (`extract_calcs`); pass `calcs=[...]` to override, or
  `calcs=[]` to emit no measures. The deterministic translator turns the safe subset into DAX and
  leaves everything else an inert `= 0` stub with the original formula preserved as a
  `TableauFormula` annotation.
- **`out["report"]`** is the audit artifact — storage-mode decision + rationale, per-measure status,
  inferred relationships, skipped tables, manual follow-ups, and `assisted_suggestions` (see §2.3).
- **`out["bind"]`** is the credential-free connection target (server/database/warehouse) the user
  needs to bind in Fabric.
- **DirectQuery Date table** uses a self-contained fixed-range `CALENDAR(...)` (override with
  `date_range=(start_year, end_year)`, e.g. from the profiler's date MIN/MAX); Import uses
  `CALENDARAUTO()`.

### Lower-level entry points

| Function | Use |
|---|---|
| `migrate_tds_to_semantic_model(tds_text, *, model_name, calcs=None, relationships=None, date_range=None, approved_calc_dax=None, ...)` | Parse + assemble from `.tds` **text** (no download/unzip, no `bind`). |
| `assemble_import_model(descriptor, *, model_name, ...)` | Assemble from an already-parsed descriptor (Import/DirectQuery). |
| `assemble_directlake_model(...)` | The landed-Delta / DirectLake fallback assembler. |
| `fabric_definition_payload(parts)` | `parts` → base64 Fabric `updateDefinition` body. |
| `write_model_folder(parts, "<Name>.SemanticModel")` | Write just the TMDL model item. |
| `write_local_pbip(parts, dest, *, model_name, report_name=None, report_parts=None)` | Write an **openable** `.pbip` (model + thin report + correct-schema pointer). |

### 2.3 Assisted-translation approval (opt-in, batch)

When a calc matches a known idiom (e.g. argmax-over-a-dimension) it is surfaced as a **non-binding
suggestion** — the measure stays `= 0` until a human approves it:

```python
pending  = out["report"]["assisted_suggestions"]          # [{measure, pattern, dax, ...}, ...]
approved = {s["measure"]: s["dax"] for s in pending}        # approve all / by pattern / a subset
final    = migrate_datasource(tds_text, model_name="...", approved_calc_dax=approved)
```

---

## 3. Deploy + refresh — `deploy_to_fabric.py`

```python
from deploy_to_fabric import acquire_token, deploy_model, refresh_dataset, FABRIC_BASE, POWERBI_BASE

fabric = acquire_token("https://api.fabric.microsoft.com", use_az=True)   # handles az.cmd on Windows
summary = deploy_model(out["parts"], model_name="Snowflake-Superstore",
                       workspace="<workspace name or GUID>", token=fabric)

# A fresh model has NO credential bound -> the first refresh fails with
# ModelRefreshFailed_CredentialsNotSpecified. That's expected: the user binds the Snowflake
# credential in Fabric (Settings -> Data source credentials, or Manage connections and gateways).
pbi = acquire_token("https://analysis.windows.net/powerbi/api", use_az=True)
status, body = refresh_dataset(summary["workspace_id"], summary["item_id"], pbi)
```

CLI: `py scripts\deploy_to_fabric.py --model-dir <...>.SemanticModel --workspace <ws> --use-az [--refresh]`
(supports `--dry-run`). Token sources: `--token` / `FABRIC_TOKEN` env / `--use-az`.

> **Credential boundary (do not cross):** never write a source password into the model, M, the
> report, or any file, and never bind it via API on the user's behalf — credentials live on a Fabric
> data connection, not in the model. Editing those credentials needs a **Pro / Fabric per-user**
> license (F2 capacity alone is not enough). See [security-governance.md](security-governance.md).

---

## Estate scale — `migrate_estate.py`

For **many** datasources/workbooks at once, see [orchestration.md](orchestration.md):
`migrate_estate(LocalFilesSource(root), out_dir)` runs the per-datasource flow across a folder and
writes a bundle + `report.json`.
