---
name: tableau-migration
description: >-
  Migrate Tableau to Microsoft Fabric / Power BI. A .tds/.tdsx datasource rebuilds as a
  semantic model: TMDL data model (typed columns + relationships), safe-subset calculated
  fields translated to DAX (original formula kept as an annotation), storage mode
  auto-selected (Import or DirectQuery). A .twb/.twbx WORKBOOK migrates whole: that model
  PLUS its dashboards/worksheets rebuilt as a model-bound Power BI (PBIR) report — chart
  type, field bindings, layout, filters/parameters as slicers — packaged as an openable
  .pbip. Use to migrate Tableau datasources or whole workbooks, convert calculated fields
  to DAX, or rebuild Tableau dashboards/worksheets as Power BI reports.
  Triggers: "migrate from tableau", "tableau to fabric", "tableau to power bi",
  "migrate tableau workbook", "tableau workbook to power bi", "tableau dashboard to power bi",
  "rebuild tableau dashboard", "tableau report to pbir", "tableau datasource to semantic model",
  "convert tableau calculation to dax".
---

> **AUTH MODEL — tableau-migration**
> **PAT (default, recommended).** Connected App (Direct Trust) **JWT only if the user explicitly
> selects D5=B.** Never silently switch auth modes or downgrade. The bundled scripts default to
> `--auth pat`; JWT requires the Connected App client/secret to be supplied on purpose.

---

## ▶ RUN CONTRACT — read before doing anything

This skill is a **gated, deterministic runbook**, not a freeform task. Follow the gates in order;
do not improvise flags or infer answers. The detailed reference body begins after the
"Run contract ends" marker further down.

### GATE RULES (non-negotiable)

1. **First turn = the Decision Menu, verbatim.** On invocation your FIRST message MUST be the
   Phase 0A Decision Menu below — issue **no** tool call, shell command, or file read in that turn.
2. **No defaults inferred, no question skipped.** Every decision (D1–D5) and every credential comes
   from the user. A blank or ambiguous answer = **STOP and ASK**, never guess.
3. **`GO` gates STEP 1+ only.** Pinning `$SKILL` / `$RUN` and writing + dot-sourcing
   `migration.vars.local.ps1` is **local setup** — it reaches nothing external and spends nothing, so
   do it *before* `GO`. Run no STEP 1/2/3 script (anything that touches Tableau, Fabric, or writes the
   bundle) until the Confirmation Ledger (Phase 0C) is filled and the user replies `GO`.
4. **A workbook input's rebuilt report is a REQUIRED output.** For a `.twb`/`.twbx` source, an openable,
   model-bound `.pbip` report ships alongside the semantic model; the run's definition-of-done ledger
   (`report.json` → `definition_of_done` + a `summary.md` banner) **fails loud** if one is missing.
5. **No deliberation in the mechanical span.** Between `GO` and the second compiler you do exactly one
   thing: run the STEP 1→3 scripts in order and read their reports. The scripts determine **everything** —
   extract vs live, embedded vs published, storage mode, flat-file landing, binding — so you never
   classify a source, pick a per-source flag, add error-handling, tune timeouts, or reason about a
   "corrective action." A checkpoint that fails = **STOP and ask**, never self-correct. The **first**
   place you are permitted to reason is the second compiler (stubbed calcs).

### Phase 0A — Decision Menu (present verbatim; defaults marked)

```
Before I migrate anything, confirm these choices (e.g. reply "D1=A, D2=all, D3=A, D4=C, D5=A, D6=A"):

D1 — SOURCE
   A) Live pull from Tableau Server/Cloud   (datasources and/or workbooks; needs Tableau creds)
   B) Local files I already have            (.tds/.tdsx datasources or .twb/.twbx workbooks)

D2 — SCOPE   (name datasources, workbooks, or both)
   all)      migrate every datasource / workbook found in .\in
   <names>)  a subset — list the datasource or workbook names

D3 — OUTPUTS  (forces both-vs-one)
   A) Fabric + local bundle   (deploy AND keep the TMDL on disk)
   B) Fabric only             (deploy, don't keep local)
   C) Local only              (build the bundle, do NOT deploy)

D4 — CONFLICTS (a model of the same name already exists in the workspace)
   A) overwrite      B) skip      C) stop and ask   [default C]

D5 — AUTH  (forces the auth choice)
   A) PAT                       (default, recommended)
   B) Connected App JWT (Direct Trust)

D6 — CREDENTIAL ACCESS  (only if D1=A; how I obtain the PAT / Connected-App secret)
   A) Azure Key Vault           (default — I read it into TABLEAU_PAT_VALUE at run time)
   B) Local secure terminal     (no Key Vault — you type it into a hidden prompt; never in chat)
```

> **A workbook's datasource migrates first, then the report binds to it — exactly like an embedded
> datasource.** You never hand-classify a workbook: `migrate_estate.py` auto-detects whether it
> **embeds** its datasource or connects to a **published** one (the STEP 1.5 scan, before any build; you
> never inspect XML) and rebuilds the report against the real schema either way. An **embedded**
> datasource rides inside the workbook, so it's already in scope and consolidated automatically — nothing
> extra to do. A **published** datasource is the *same flow* with one extra step: it lives outside the
> workbook (only a `sqlproxy` stub travels along), so the STEP 1.5 scan names it *before* building —
> fetch that datasource by name into scope and re-scan, then it migrates first and the workbook binds to
> it, just like embedded.
>
> **For local output (D3=C, and the local bundle of D3=A), the STEP 1.5 scan auto-detects immediately —
> you never ask.** It tells you embedded or published; the only hard rule is that a published
> datasource's data must be in scope so the report binds. If the scan names one that isn't in scope yet,
> fetch that name and re-scan so it lands first. There is no workbook-only migration: a published-backed
> workbook whose datasource is missing rebuilds to an **empty report** — the scan gate stops you before
> you ever ship that empty result.
>
> **Fabric outputs (D3=A/B) are a *different decision tree*** — the published datasource may already
> exist in the target workspace as a semantic model, so it's **bind-to-existing vs. migrate-fresh**
> rather than an automatic co-migrate. Handle that deliberately at deploy (STEP 3, which flags
> duplicate models); do **not** apply the local "always migrate first" rule to a Fabric deploy.

### Phase 0B — Credentials form (simple 2-file pattern)

**Ask D6 first (only when D1=A): "How would you like me to access the Tableau credentials —
(A) Azure Key Vault, or (B) a local secure terminal prompt?"** Never accept a secret pasted into
chat under either choice.

Collect the values below, then write them into a **git-ignored** local vars file — never paste the
PAT/Connected-App secret into chat.

| Variable | Meaning |
|---|---|
| `SITE_URL` | Tableau host, e.g. `10ay.online.tableau.com` (no `https://`) |
| `SITE_NAME` | site contentUrl (URL slug; empty string for Default) |
| `PAT_NAME` | Personal Access Token name (D5=A) |
| `KV_NAME` | Azure Key Vault holding the secret value (**D6=A only**) |
| `SECRET_NAME` | the Key Vault secret whose value is the PAT (or Connected-App) secret (**D6=A only**) |
| `FABRIC_WORKSPACE` | target Fabric workspace name or GUID (only if D3 ≠ C) |
| `SUBSCRIPTION_ID` | *(optional)* Azure subscription that holds the Key Vault / Fabric capacity — set **only** if your `az` CLI default subscription isn't already it (**D6=A or `--use-az`**) |

If **D5=B**, also collect the Connected App `CLIENT_ID`, `SECRET_ID`, and impersonation
`JWT_USERNAME` (the secret value comes from Key Vault or the terminal prompt) instead of `PAT_NAME`.

**Working directory — pin once (before anything touches disk; no `GO` needed — this is local setup).**
Set two paths and run **every** later command from `$RUN`:
- **`$SKILL`** = the folder holding this SKILL.md (where `scripts\` and `migration.vars.example.ps1`
  live — the folder you loaded these instructions from).
- **`$RUN`** = a **fresh, empty** working folder for THIS migration — **you don't name or create it by
  hand**. The bundled **`new_run.py`** mints the next one and **prints its path**; set `$RUN` to that
  (the code block below does exactly this). It auto-increments a clean `C:\tfmig\runs\NNNN\` with empty
  `in\`+`out\` already made, so a **reused root can never carry stale `in\`/`out\` from a prior run** (a
  recurring foot-gun) and you never have to hunt for "the next free number". Its default root is
  deliberately **SHORT and near the drive root** (`C:\tfmig`) — **not** the deep session/temp path —
  because a rebuilt workbook's PBIR report nests many folders deep
  (`…\<Workbook>.Report\definition\pages\<page>\visuals\<visual>\visual.json`), so a long root can push
  a file past the Windows **MAX_PATH (260)** limit. The build itself handles this — deep writes and the
  Fabric deploy use Windows extended-length (`\\?\`) paths, so a long root **no longer fails the build**
  (and a long root deployed straight to Fabric is fine). The reason to keep it short is the **local**
  `.pbip`: to open it in **Power BI Desktop** on a deep path you'd otherwise need Windows long paths
  enabled, so a short root keeps the local project openable everywhere. (Only a genuinely unwritable path
  still marks that workbook `failed` with a `path_too_long` reason in `report.json`.) To pin a different
  root, pass `--root <dir>` to `new_run.py`.

`in\` (fetched inputs) and `out\` (the built bundle) live **under `$RUN`, never under `$SKILL`** — that
keeps the run clear of the skill's bundled sample datasources and never pollutes the installed skill.
Every script call is `py -3.11 "$SKILL\scripts\<name>.py"`.

Then set up the local vars file (mirrors the repo's `.env.example` → `.env` convention):

```powershell
$SKILL = "<the folder holding this SKILL.md>"
$RUN   = (py -3.11 "$SKILL\scripts\new_run.py" --root C:\tfmig)   # auto-mints a clean C:\tfmig\runs\NNNN (empty in\+out\) and prints its path
Set-Location $RUN
Copy-Item "$SKILL\migration.vars.example.ps1" .\migration.vars.local.ps1   # once
# fill migration.vars.local.ps1 with the real values (it is git-ignored), then:
. .\migration.vars.local.ps1
if ($SUBSCRIPTION_ID) { az account set --subscription $SUBSCRIPTION_ID }   # only if your az default sub isn't the one holding the KV / Fabric capacity
```

`migration.vars.example.ps1` is committed with **placeholders**; `migration.vars.local.ps1` holds
the **real** values and is git-ignored — never commit or mirror it.

> **D6=B — Local secure terminal (no Azure Key Vault).** Key Vault is the default, but it is **not
> required**. When the user chooses the local terminal, run `fetch_tds.py` with **`--prompt-secret`**
> (or simply leave `TABLEAU_PAT_VALUE` unset): it asks for the PAT secret at a **hidden** `getpass`
> prompt in that terminal, exchanges it for a session token, and clears it from the process
> environment afterward. The secret is held **in memory only** — never echoed, written to disk
> (`.env`, logs, the report), or placed in chat — and an empty entry is rejected (fail fast). Tell
> the user explicitly: *"enter your PAT secret in the terminal now; don't paste it in chat."* This
> is the same masked path the layered, Key-Vault-free resolver (`scripts/credential_resolver.py`)
> exposes — which also supports `TABLEAU_PAT_VALUE`, a git-ignored `.env`, or an OS keyring
> (`pip install keyring`) when those are preferred. Use `--no-prompt` for unattended/CI runs.

### Phase 0C — Confirmation Ledger (the run gate)

Echo the resolved choices + resources back, then wait for `GO`:

```
LEDGER — confirm, then reply GO
  work dir   : <$RUN>   (scripts run from here; skill at <$SKILL>)
  source     : <D1 A live / B local>   from <SITE_URL/SITE_NAME  or  .\in>
  scope      : <all | datasource and/or workbook names>
  workbook ds: auto-detected at STEP 1.5 (scan) — never ask the user, never hand-classify (the engine reads the workbook itself)   (omit if no workbook. Embedded → consolidated automatically; published → its datasource is co-migrated FIRST so the report binds. The STEP 1.5 scan names any published datasource that isn't in scope yet and exits non-zero; fetch that name into `.\in` and re-scan until it exits 0 BEFORE STEP 2 — never build an empty workbook-only rebuild.)
  outputs    : <D3 A both / B Fabric only / C local only>
  conflicts  : <D4 overwrite | skip | stop>
  auth       : <D5 PAT | Connected App JWT>   (D6 secret via <Key Vault KV_NAME/SECRET_NAME | local terminal prompt>)
  fabric ws  : <FABRIC_WORKSPACE>             (omit if D3=C)
```

Run nothing until the user replies `GO`.

### The 3-step runbook (literal flags — do not alter)

> Flags below are exactly what the bundled scripts accept (`--help`-verified). `fetch_tds.py`
> downloads **one datasource per call** (there is no `--all`) and writes with `--out`;
> `migrate_estate.py` takes `-i/-o` and emits `<out>/semantic_models/<Name>.SemanticModel` +
> `report.json` + `summary.md`; `deploy_to_fabric.py` deploys **one** `--model-dir` per call.

PowerShell (Windows lead). Run every command from `$RUN` (pinned in Phase 0B); call scripts as
`"$SKILL\scripts\…"`. Starting a fresh shell? Re-run `Set-Location $RUN ; . .\migration.vars.local.ps1`
first.

**STEP 1 — assemble `.\in`: get every scoped name into the folder (one `fetch_tds.py` call each)**

Every name in D2 scope — datasource **or** workbook — is fetched the same way, always with
`--include-extract`. You do **not** classify sources, choose per-source flags, or decide
embedded-vs-published; STEP 2 auto-detects all of that.

- **D1=B (local):** drop the exported files into `.\in` (`.tds`/`.tdsx` **datasources** and/or
  `.twb`/`.twbx` **workbooks**; export the **packaged** `.tdsx`/`.twbx` so any extract/flat-file data
  travels inside). A packaged export and its unpacked twin of the **same stem** (`Sales.tdsx` +
  `Sales.tds`) count as **one** asset — dropping both is safe. Go to STEP 2.
- **D1=A (live):** run this block exactly — fill the two name lists from D2 scope (leave a list empty
  if that kind isn't in scope):

```powershell
$env:TABLEAU_PAT_VALUE = az keyvault secret show --vault-name $KV_NAME --name $SECRET_NAME --query value -o tsv   # D6=A only; omit this line for D6=B
New-Item -ItemType Directory -Force -Path .\in | Out-Null
foreach ($name in @("<Datasource A>","<Published DS>")) {   # D2 datasource scope
  py -3.11 "$SKILL\scripts\fetch_tds.py" --server $SITE_URL --site $SITE_NAME `
    --datasource-name $name --include-extract --auth pat --pat-name $PAT_NAME --out .\in
}
foreach ($name in @("<Workbook A>")) {                       # D2 workbook scope
  py -3.11 "$SKILL\scripts\fetch_tds.py" --server $SITE_URL --site $SITE_NAME `
    --workbook-name $name --include-extract --auth pat --pat-name $PAT_NAME --out .\in
}
```

> **One process, one call — read the secret and fetch in the SAME call.** Every PowerShell/terminal call
> is a **fresh process**, so an env var set in one call (like `TABLEAU_PAT_VALUE` from the `az keyvault`
> line) is **gone** by the next. Run the `az keyvault` read and the fetch loop as the **single block
> above** — never "verify" the Key Vault read in its own separate call (that read evaporates and you just
> re-do it).

`--include-extract` is **always on** — required for an extract/flat-file source, harmless on a live DB
source (Tableau returns the `.tds` with no `.hyper`) — so it is never a per-source decision. Auth/secret
flags come straight from the menu, never re-derived here: **D6=B** → omit the `az keyvault` line, leave
`TABLEAU_PAT_VALUE` unset, add `--prompt-secret` to each call (user types the PAT in the terminal, never
in chat). **D5=B (JWT)** → replace `--auth pat --pat-name $PAT_NAME` with
`--auth jwt --client-id $CA_CLIENT_ID --secret-id $CA_SECRET_ID --jwt-username $JWT_USERNAME`. A name
with spaces, parentheses, or an apostrophe (e.g. `Sales (Lod's)`) is fine inside the `@("…")` list — it
is already a quoted PowerShell string; only a literal `"` inside a name must be doubled (`""`).

> **⛔ The one and only download path — do not improvise around a perceived gap.** `fetch_tds.py` is the
> sole downloader: never hand-roll a Tableau REST call, never unzip a `.twbx` (STEP 2 ingests packaged
> files directly). A workbook is just another name to fetch (`--workbook-name`); whether it embeds its
> datasource or connects to a **published** one, the STEP 1.5 scan auto-detects and binds it — you never
> inspect XML or read a connection class. The **only** requirement is that a published workbook's
> datasource is itself a name in your D2 scope, so it's already in the datasource list above and the two
> migrate together. If the STEP 1.5 scan names a published datasource that is **not** yet in scope,
> **fetch that name into `.\in` and re-scan** so it migrates first and the workbook binds — never
> hand-roll a workaround.

**Checkpoint 1:** `.\in` holds one file per scoped name — and **only** those. `migrate_estate.py` has
**no name filter**: it migrates *everything* in `-i`, so scope is enforced solely by what's in `.\in` (a
fresh `$RUN\in` can't pick up the skill's bundled samples). Fewer files than scoped, or any extra →
**STOP and ask** (do not re-derive or substitute names).

**STEP 1.5 — scan first: never build a workbook before its datasource is in scope**

Before building, run the read-only scan (no creds, no build, no unzip). It reports, per workbook,
whether it binds to a **published** datasource and whether that datasource is already in `.\in`, and it
**exits non-zero** while any published datasource is missing:

```powershell
py -3.11 "$SKILL\scripts\migrate_estate.py" -i .\in -o .\out --scan
```

For every name under `missing_published_datasources` in `.\out\scan.json` (echoed on the `[ACTION]`
line), fetch it into `.\in` with the **same** `fetch_tds.py` call as STEP 1, then **re-scan** — repeat
until the scan prints `[OK]` and exits `0`:

```powershell
py -3.11 "$SKILL\scripts\fetch_tds.py" --server $SITE_URL --site $SITE_NAME `
  --datasource-name "<missing name>" --include-extract --auth pat --pat-name $PAT_NAME --out .\in   # D1=A live
# D1=B local: drop that published datasource's packaged .tdsx into .\in instead, then re-scan
```

> **⛔ Hard gate — do not run STEP 2 until the scan exits `0`.** A published-backed workbook built while
> its datasource is missing rebinds to nothing and ships an **empty report**; the scan exists to stop
> exactly that, *before* any build. Datasource-only runs and embedded-datasource workbooks scan clean
> immediately (nothing to fetch). This is still not "inspecting XML" — the script reads the workbook and
> tells you; you only fetch the name it prints.

**Checkpoint 1.5:** `.\out\scan.json` shows `missing_published_datasources: []` and the command exited
`0`. If not, fetch the named datasource(s) and re-scan — never proceed to STEP 2 with a missing one.

**STEP 2 — build the Fabric bundle**

```powershell
py -3.11 "$SKILL\scripts\migrate_estate.py" -i .\in -o .\out
```

**Checkpoint 2:** confirm `.\out\semantic_models` holds one `*.SemanticModel` per datasource,
`.\out\report.json` shows `summary.datasources_migrated > 0`, and every workbook source has an openable
`.\out\pbip\<Workbook>.pbip` bound to its model. A workbook whose report is empty or unbound almost
always means its published datasource wasn't in scope at build time — which the STEP 1.5 scan gate is
designed to prevent, so re-run the scan and confirm it exits `0` before anything else. Anything missing
or `0` → **STOP**, read `.\out\summary.md`, and report it to the user. Do not self-diagnose, re-fetch,
or re-run — the scripts have already done their own detection and binding; a shortfall is a
STOP-and-ask, never something for you to fix by hand.

> **Second-compiler gate — read `report.json` → `summary.needs_review_total` and branch on it:** if it
> is **`0`**, no calc was left stubbed, so there is nothing to offer — continue (do not re-read the report
> to "make sure"). When it is **`> 0`**, **STOP and OFFER the second compiler**: show the user the stubbed
> calcs (count + names) and ask whether to run the LLM-assisted second-compiler pass. Run it **only** on an
> explicit `GO`; if the user declines, ship the deterministic result as-is — every stub keeps its preserved
> `TableauFormula` (a complete, honest outcome). See *Post-Migration* step 3 /
> [second-compiler.md](resources/second-compiler.md).

**STEP 3 — deploy (skip entirely if D3=C / local only)**

Deploy each model folder (one `--model-dir` per call):

```powershell
Get-ChildItem .\out\semantic_models -Directory | ForEach-Object {
  py -3.11 "$SKILL\scripts\deploy_to_fabric.py" --model-dir $_.FullName --workspace $FABRIC_WORKSPACE --use-az
}
```

The deployed **model name** defaults to the `.SemanticModel` folder stem — a filesystem-safe form of the
datasource name (spaces / parens / apostrophes may become `_`, sometimes doubled, e.g.
`Databricks_Example_-_Tier_1__Lod_s_`). For a clean display name, add `--model-name "<Real Name>"` to
that model's call.

> **Avoid duplicate models for the same datasource.** The name a datasource gets depends on **how** it was
> migrated: **standalone** it takes the sanitized folder stem above; **inside a workbook** it lands in that
> workbook's consolidated model (named from the workbook / its primary datasource). So deploying both — or
> re-running the other way — can create **two near-duplicate models** for one datasource. If a model for
> this datasource already exists in the workspace, pass `--model-name "<the existing model's name>"` so the
> deploy **overwrites** it instead of spawning a duplicate.

D4 handling — the script does **createOrUpdate only** (it always overwrites a same-named model; there is
no skip/error flag). **D4=A (overwrite):** the loop above is correct as-is. **D4=B (skip) / D4=C (stop):**
list the workspace first and branch yourself — pre-fetch existing model names, then for B exclude those
folders from the loop, and for C halt and ask on the first name that already exists. If a model binds an
on-prem source, add `--gateway-id <id>`.

> Each deploy also fires a **credential-free ProcessRecalc** by default so the model opens without
> benign "needs refresh" warning triangles (see *After deploy: the credential-binding wall* below). It is
> **best-effort and asynchronous — started, not polled to completion** (unlike the model-deploy LRO that
> Checkpoint 3 waits on), so its `202` means *accepted*, not *finished*. Pass `--no-recalc` to skip it.

**Checkpoint 3:** each deploy completes its **model-deploy LRO** without error (the follow-on ProcessRecalc
is best-effort/async — started, not awaited; a non-`2xx` there is logged non-fatal, not a stop). Any deploy
failure → **STOP and ask**, do not continue or retry with altered flags.
If D3=B (Fabric only), remove `.\out` after a clean deploy; if D3=A, keep it. Either way the fetched
inputs in `.\in` are **sensitive** (they carry the datasource's connection details and any embedded
extract data) — delete `.\in` once the run is verified, unless the user asked to keep it. Never commit
`.\in` or `.\out`.

bash equivalent: same flags with `python3.11` instead of `py -3.11`; export the same variables in
your shell (or a local, git-ignored file you `source`) and read the secret with
`az keyvault secret show --vault-name "$KV_NAME" --name "$SECRET_NAME" --query value -o tsv` into
`TABLEAU_PAT_VALUE`.

**STEP 4 — value reconciliation (post-bind; the check that makes "migrated" mean "numbers match")**

Deploy proves the model is **structurally** correct; it does **not** prove the numbers match Tableau. That
proof is **value reconciliation** — `executeQueries` DAX vs the Tableau VDS — and it is **blocked until the
human credential bind** (see *After deploy: the credential-binding wall* below), because the model can't be
queried until its connection is bound. So **queue it as an explicit follow-up**, never skip it silently: the
moment credentials are bound, run the reconciliation per [validation-reconciliation.md](resources/validation-reconciliation.md).
Until then, report the model as **structurally migrated — numbers not yet verified**, never as "verified."

<!-- ===== Run contract ends; detailed reference body below ===== -->

---

> **Updating this skill — only when the user asks**
> There is **no** mandatory per-session update check. When the user asks to *check for updates / update / upgrade / refresh the `tableau-migration` skill* (or "update yourself"), follow [`resources/self-update.md`](resources/self-update.md). It is a **version-aware reinstaller**, not a guess:
> - **Source of truth:** repo `https://github.com/Yarbrdab000/tableau-fabric-skills`, skill subpath `skills/tableau-migration`, version stamp `skills/tableau-migration/VERSION`. **Install target:** the folder this `SKILL.md` was loaded from (canonical); `~/.copilot/skills/tableau-migration` is a manual-only fallback.
> - **Compare, then act:** read installed `VERSION` → read remote `VERSION` → only reinstall if remote is newer (or the user forces). Install is an **explicit wholesale overwrite** (`scripts/` + `resources/` + `SKILL.md` + `VERSION`), then a **fail-loud verification** (assert `migrate_datasource` / `extract_calcs` / `fetch_tds` exist + run `pytest`; on failure, restore the backup and stop). Finish by reporting the delta (e.g. `1.2.1 → 1.3.0`).
> - **Mid-session caveat:** skills load at session start, so the update is not live until a **new** session.

> **CRITICAL NOTES**
> 1. To find the workspace details (including its ID) from a workspace name: list all workspaces, then use JMESPath filtering.
> 2. To find the item details (including its ID) from workspace ID, item type, and item name: list all items of that type in that workspace, then use JMESPath filtering.
> 3. **Column types are driven by the source schema, never guessed.** The DirectLake path types columns from the landed Delta schema; the Import/DirectQuery path types them from the Tableau `.tds` `<metadata-records>`. A datasource with no resolvable column metadata falls back to the land-to-Delta path — it is never deployed with inferred types.
> 4. **Calculated-field translation is a deterministic safe subset, not full coverage.** Anything outside the subset stays an inert `= 0` stub; the original Tableau formula is ALWAYS preserved as a `TableauFormula` annotation so a human (or an optional validation-gated LLM pass) can finish it. Never claim full DAX parity.
> 5. **Credentials and on-premises gateways are a manual security boundary.** This skill emits the model, the connection parameters, and the structured **bind inputs** (`connection_details_for_bind`), and can deploy the model itself via the bundled `scripts/deploy_to_fabric.py` (or delegate to `semantic-model-authoring`) — but the user enters credentials and selects/sets up the gateway. On a credential error, stop and have the user configure the connection.

# Tableau → Microsoft Fabric Migration — semantic models + rebuilt dashboards

This skill packages a proven Tableau → Fabric toolkit as a reusable migration skill. The **north star
is estate-wide rebuild** — point at a Tableau deployment and rebuild its datasources, calculated fields,
and workbooks as equivalent Fabric / Power BI assets, with **executed reconciliation** verifying the
numbers actually match. **A datasource** rebuilds as a semantic model (data model + relationships,
calculated fields → DAX, connection wired). **A workbook** migrates as a whole: that semantic model
**plus** its dashboards/worksheets rebuilt as a model-bound Power BI (PBIR) report — Tier-1 *structure*
(chart type, exact field bindings, position/layout, filters/parameters → slicers, default cross-filter,
structural titles/axis names) — packaged as an openable, model-bound `.pbip`. This report rebuild is a
**default deliverable**, not an add-on: the run's definition-of-done fails loud if a workbook lands no
bound report. **Deferred to a later pass:** model-object enrichment (hierarchies / display folders / RLS)
and visual *formatting* (specific colors, fonts, legends, conditional formats). See
[§ Feature Parity](#feature-parity-reference) for current vs. in-progress coverage.

## Inputs — Locate the Datasource FIRST

> **The datasource to migrate is supplied by the user. Do NOT assume it lives in the current repo or working directory.** This skill is the migration *toolkit*, not a datasource — a fresh checkout contains no `.tds`. Do **not** search the working directory, find nothing, and stall. Before any other phase, establish the input by asking the user which route applies:
>
> - **(A) Local file** *(simplest — no Tableau credentials)* — the user has a Tableau file. Ask for the path to a `.tds`, `.tdsx`, `.twb`, or `.twbx`. `.tdsx`/`.twbx` are zips: extract the inner `.tds`/`.twb` first. Always read with `encoding="utf-8-sig"` (the files carry a UTF-8 BOM).
> - **(B) Live published datasource** — the user names a datasource published on Tableau Server / Cloud (a *name*, not a file path). Pull it down first with the **`tableau-datasource-profiler`** skill (or the Tableau **Download Data Source** REST API + Metadata API) using a PAT or Connected-App JWT; that yields the `.tds` this skill consumes, plus field/lineage metadata and reconciliation values.
>
> If the user just says "migrate my Tableau datasource" without specifying, **ask which route** (file path vs. published-datasource name + Tableau connection) rather than guessing. Once you hold the `.tds`, continue to the Migration Phases below.
>
> **Workbooks may embed several datasources.** A `.twb`/`.twbx` can contain more than one datasource (worksheet reference stubs and the `Parameters` pseudo-datasource are ignored). Call `list_workbook_datasources(source)` (or `workbook_datasources(xml)`) to enumerate the real ones; if there's exactly one, it's used automatically, otherwise pass `datasource="<name>"` to `migrate_datasource` to pick. Selecting an ambiguous workbook without a `datasource=` raises `AmbiguousDatasourceError` listing the choices.
>
> **Migrating a whole workbook (model + report together).** `migrate_datasource` is datasource-scoped — it builds the *model* for one datasource and never rebuilds the workbook's report. To rebuild an entire workbook as an openable project — its embedded datasource(s) **and** the report bound to them — call **`migrate_workbook(source, write_to=…)`** (in `migrate_estate.py`). It is the single-workbook form of `migrate_estate` (the estate loops it per workbook), so one workbook and a whole estate share one code path; a multi-datasource workbook consolidates **all** its embedded datasources into one model (disconnected table islands, each bound to its own connection) with a single report bound to it. Prefer it over `migrate_datasource` whenever the input is a workbook and you want the report, not just a datasource model.

## Prerequisite Knowledge

This skill is **self-contained** — the bundled scripts cover the full migration (parse → TMDL → calc→DAX → connection → deploy). Fabric token audiences and the deploy REST flow are documented inline below and in `scripts/deploy_to_fabric.py`. When the optional peer skills (`semantic-model-authoring`, `semantic-model-consumption`) are installed alongside this one, they provide deeper general-Fabric REST / `az` references and best-practice analysis — but they are **not required**.

> **This skill can deploy the model itself via the bundled `scripts/deploy_to_fabric.py`, or delegate model deploy / edit / refresh / best-practice analysis and connection binding to the `semantic-model-authoring` skill, with DAX round-trip validation via `semantic-model-consumption` (FabricIQ `ExecuteQuery`).** It owns the Tableau-side reconstruction (datasource → TMDL, calc → DAX, connection → M).

---

## Table of Contents

| Topic | Reference |
|---|---|
| **Migration Orchestrator** | [migration-orchestrator.md](resources/migration-orchestrator.md) |
| API-Driven Migration Workflow | [§ API-Driven Migration Workflow](#api-driven-migration-workflow) |
| Migration Phases (ordered) | [§ Migration Phases](#migration-phases-execute-in-order) |
| Migration Workload Map | [§ Migration Workload Map](#migration-workload-map) |
| Storage-Mode Selection (extract/live/custom-SQL) | [storage-mode-selection.md](resources/storage-mode-selection.md) |
| Semantic Model Rebuild (TMDL, types, relationships) | [semantic-model-rebuild.md](resources/semantic-model-rebuild.md) |
| Calculated Field → DAX | [calc-to-dax.md](resources/calc-to-dax.md) |
| Second Compiler (Tier-1 assisted translation) | [second-compiler.md](resources/second-compiler.md) |
| Tier-1 Charter (Tier-0 vs Tier-1 boundary) | [tier1-charter.md](resources/tier1-charter.md) |
| Connection → M Partition & Binding | [connection-binding.md](resources/connection-binding.md) |
| Validation & Reconciliation (ExecuteQuery vs VDS) | [validation-reconciliation.md](resources/validation-reconciliation.md) |
| Migration Gotchas | [migration-gotchas.md](resources/migration-gotchas.md) |
| Security & Governance | [security-governance.md](resources/security-governance.md) |
| Migration Report | [migration-report.md](resources/migration-report.md) |
| Updating / upgrading this skill | [self-update.md](resources/self-update.md) |
| Feature Parity Reference | [§ Feature Parity Reference](#feature-parity-reference) + [feature-parity.md](resources/feature-parity.md) |
| Must / Prefer / Avoid | [§ Must / Prefer / Avoid](#must--prefer--avoid) |

### Context Loading Guide

> **IMPORTANT — Load only what you need.** Do NOT read all resource files upfront. Load the specific file for the phase you are executing:

| When | Read This File | Lines |
|---|---|---|
| User asks to migrate a datasource (full orchestration) | [migration-orchestrator.md](resources/migration-orchestrator.md) | ~210 |
| Deciding storage mode for a datasource | [storage-mode-selection.md](resources/storage-mode-selection.md) | ~150 |
| Generating the TMDL model (types, columns, relationships) | [semantic-model-rebuild.md](resources/semantic-model-rebuild.md) | ~180 |
| Translating calculated fields | [calc-to-dax.md](resources/calc-to-dax.md) | ~200 |
| A calc fell back / handling the Tier-1 handoff | [second-compiler.md](resources/second-compiler.md) | ~200 |
| Why a construct is Tier-0 vs Tier-1 (the boundary) | [tier1-charter.md](resources/tier1-charter.md) | ~190 |
| Emitting M partitions / binding the connection | [connection-binding.md](resources/connection-binding.md) | ~170 |
| Validating the migrated model | [validation-reconciliation.md](resources/validation-reconciliation.md) | ~140 |
| Troubleshooting failures | [migration-gotchas.md](resources/migration-gotchas.md) | ~120 |
| Production security setup | [security-governance.md](resources/security-governance.md) | ~110 |
| Generating the migration report | [migration-report.md](resources/migration-report.md) | ~90 |
| User asks to **update / upgrade this skill** | [self-update.md](resources/self-update.md) | ~110 |
| Feature parity / what is NOT migrated | [feature-parity.md](resources/feature-parity.md) | ~80 |

### Bundled Scripts

The pure-Python cores are offline, deterministic, and stdlib-only (no Spark / pandas required to run them):

| Script | Purpose |
|---|---|
| [`scripts/fetch_tds.py`](scripts/fetch_tds.py) | **Tableau-side download** (stdlib-only): REST sign-in (PAT **or** Connected-App JWT), find a published **datasource _or_ workbook** by name (or LUID), download it, and extract the inner `.tds` from a `.tdsx` (`inner_tds_from_zip`) **or** the inner `.tds`/`.twb` from any Tableau archive incl. `.twbx` (`inner_doc_from_zip`). CLI (`--datasource-name`/`--datasource-luid`/`--workbook-name`/`--workbook-luid`, `--include-extract`, `--out`) **and** importable (`sign_in`, `resolve_datasource_luid`, `download_datasource`, `resolve_workbook_luid`, `download_workbook`). Use this instead of hand-writing Tableau REST. |
| `calc_to_dax.py` | Deterministic, typed Tableau calc → DAX translator. Recursive-descent parser: single-field aggregations + arithmetic, `IF`/`ELSEIF`/`IIF` conditionals, comparison + `AND`/`OR`/`NOT`, and `ZN`/`IFNULL`/`ISNULL`; `None` on fallback. Plus `suggest_assisted_dax` — idiom suggestions (e.g. argmax-over-a-dimension) the second compiler validates and lands automatically, never silently live before validation. |
| [`scripts/translation_router.py`](scripts/translation_router.py) | **Tier-0 → Tier-1 support layer** (pure, dependency-free). `classify_fallback(reason, role, fields)` — the **router** — maps the deterministic engine's honest `fallback_reason` to a stable charter category (`model_object_parameter` / `missing_addressing_intent` / `missing_outer_aggregation` / `dax_language_gap` / `type_or_shape_mismatch` / `unresolved_reference` / `unsupported_other`) + agent guidance; drives `translation_handoff` (the second-compiler input). `check_candidate_dax(dax, request)` — the **syntactic gate** — vets a second-compiler candidate (balanced parens/brackets/quotes, not an inert stub, no leftover `{FIXED}`/`[Parameters]` idioms) before it may land. See [second-compiler.md](resources/second-compiler.md). |
| [`scripts/tmdl_generate.py`](scripts/tmdl_generate.py) | TMDL generators: typed columns, tables, measures, relationship inference, model files. |
| [`scripts/field_resolver.py`](scripts/field_resolver.py) | Unambiguous caption → column resolver for the DirectLake (landed-Delta) path. |
| [`scripts/storage_mode.py`](scripts/storage_mode.py) | Per-datasource storage-mode auto-selection (pure policy). |
| [`scripts/connection_to_m.py`](scripts/connection_to_m.py) | Parse Tableau `.tds`/`.twb` → descriptor (`parse_tds(text, select=None)`); **`extract_calcs`** (calculated fields → `calcs=`); **`workbook_datasources`** (list selectable datasources, skipping `Parameters` + worksheet stubs); emit M partitions + bind details (`connection_details_for_bind`); M-path field resolver. |
| [`scripts/assemble_model.py`](scripts/assemble_model.py) | Tier-1 orchestrator: `.tds`/`.twb` → full Fabric SemanticModel definition (TMDL parts + `.platform` + `.pbism`), base64 deploy payload. **One-call `migrate_datasource(.tdsx/.tds/.twbx/.twb/text, datasource=None)` → `{parts, report, bind}`** (auto-extracts calcs; `datasource=` selects from a multi-datasource workbook; a genuine fallback returns `parts={}` + `report["landing_plan"]` via `directlake_landing_plan`); `list_workbook_datasources`, `write_model_folder` / **`write_local_pbip`** for local output. |
| [`scripts/migrate_estate.py`](scripts/migrate_estate.py) | **Estate + workbook orchestrator.** `migrate_estate(source, out)` migrates a whole folder / site (every datasource + workbook) in one run. **`--scan`** is a read-only pre-build gate: it writes `<out>/scan.json` naming each workbook's published datasource and whether it's in scope, and **exits non-zero** while any is missing (run it before building so a published-backed workbook is never rebuilt to an empty report — see STEP 1.5). **`migrate_workbook(source, write_to=…, name=None)`** is the single-workbook primitive the estate loops: it rebuilds the workbook's embedded datasource(s) into one semantic model **and** the workbook's report bound to it — an openable `pbip/<Name>/` (plus a bare `reports/<Name>.Report`); a multi-datasource workbook consolidates every embedded datasource into one model (disconnected table islands, each bound to its own connection) with a single report bound to it. Reach for it (over `migrate_datasource`) whenever the input is a **workbook** and you want the **report**, not just a datasource model. |
| [`scripts/deploy_to_fabric.py`](scripts/deploy_to_fabric.py) | Self-contained Fabric REST deploy (stdlib-only urllib): createOrUpdate / updateDefinition of the SemanticModel, 202 LRO polling, optional refresh + gateway bind. **Also deploys the workbook's REPORT** as a Fabric `reports` item — `deploy_pbip` / `deploy_report` + the fail-closed `rebind_report_byConnection` (rewrites `definition.pbir` to a **`byConnection`** `semanticmodelid=<id>` reference, required for REST deploy) via `--pbip` / `--report-dir`. Importable `acquire_token` (handles `az` on Windows) + `refresh_dataset` / **`recalc_dataset`** (a default, credential-free `type: Calculate` ProcessRecalc that processes the Import calc tables so a composite model opens without benign warning triangles; `--no-recalc` to skip) for post-deploy ops. Lets the skill finish **in Fabric** without depending on a peer skill. |

For exact signatures and a copy-paste **download → migrate → deploy** snippet, see [public-api.md](resources/public-api.md).

Run the test suite with `pytest` from `skills/tableau-migration/` (900+ offline assertions).

---

## API-Driven Migration Workflow

This skill rebuilds Tableau artifacts via REST APIs — no Tableau or Fabric UI required.

### Authentication

| Target | Token Audience |
|---|---|
| Tableau REST / Metadata / VizQL Data Service | Tableau PAT or Connected-App JWT (per the Tableau server) |
| Fabric REST API (deploy, bind) | `https://api.fabric.microsoft.com` |
| Power BI dataset refresh | `https://analysis.windows.net/powerbi/api` |

> The bundled `scripts/deploy_to_fabric.py` acquires the Fabric / Power BI token for you (`--token`, the `FABRIC_TOKEN` env var, or `--use-az` → `az account get-access-token`). Tableau tokens come from the source Tableau Server/Cloud.

> **Source extraction**: the Tableau **Download Data Source** REST API returns a `.tds` (or `.tdsx` zip) — the authoritative source for connection class, server, database, relations, and column types. The **Metadata API** (GraphQL) supplies datasource/field/lineage metadata. The **VizQL Data Service** supplies real values used for reconciliation. Treat all downloaded artifacts as **sensitive plaintext**.

### Migration Phases (Execute in Order)

| Phase | Tableau Source | Fabric Target | Resource |
|---|---|---|---|
| Phase 0 | Connectivity (REST/Metadata/VDS auth) | — | [migration-orchestrator.md](resources/migration-orchestrator.md) |
| Phase 1 | Datasource metadata + `.tds` connection | Normalized descriptor | [storage-mode-selection.md](resources/storage-mode-selection.md) |
| Phase 2 | Datasource shape → storage mode | Import / DirectQuery / DirectLake decision | [storage-mode-selection.md](resources/storage-mode-selection.md) |
| Phase 3 | Schema + fields | TMDL tables, typed columns, relationships | [semantic-model-rebuild.md](resources/semantic-model-rebuild.md) |
| Phase 4 | Calculated fields | DAX measures (+ preserved formula annotations) | [calc-to-dax.md](resources/calc-to-dax.md) |
| Phase 5 | Connection | M partitions + Fabric connection bind | [connection-binding.md](resources/connection-binding.md) |
| Phase 6 | Deploy & refresh | Semantic model **+ report** (bundled `scripts/deploy_to_fabric.py` — `--pbip` deploys the model then the report rebound `byConnection`; or delegate the model to `semantic-model-authoring`) | [migration-orchestrator.md](resources/migration-orchestrator.md) |
| Final | Validation & reconciliation | Verified model | [validation-reconciliation.md](resources/validation-reconciliation.md) |
| Optional | Security & Governance | — | [security-governance.md](resources/security-governance.md) |

> **Phase order matters**: the storage-mode decision (Phase 2) determines how columns are typed (Phase 3) and how the connection is wired (Phase 5). The DirectLake fallback path additionally requires the data to be landed as Delta first.

---

## Migration Workload Map

| Tableau Component | Fabric / Power BI Target | Notes |
|---|---|---|
| **Published Data Source** (`.tds` / `.tdsx`) | **Semantic Model** (TMDL) | The core migration unit. |
| **Physical table relation** | **Model table + partition** | One table per relation; storage mode per [storage-mode-selection.md](resources/storage-mode-selection.md). |
| **Extract** (`.hyper`) | **Import** model | Snapshot-to-snapshot; live DirectQuery offered as an alternative when the source is supported. |
| **Live connection** (SQL Server/Snowflake/Postgres/…) | **DirectQuery** model | Live-to-live via an M partition + Fabric Data Connection. |
| **Custom SQL** in a connection | **`Value.NativeQuery`** partition | Native query preserved with `[EnableFolding=true]`. SQL Server family folds against the database handle; Databricks folds against a drilled `Kind="Database"` catalog handle (never the `Catalogs()` root) and the output is aliased back to the model's `sourceColumn`s. Other connectors (e.g. Snowflake) emit a deploy-valid scaffold flagged `needs_review` for manual completion. |
| **Calculated field** (safe subset) | **DAX measure** | Aggregations (`SUM/AVG/MIN/MAX/MEDIAN/COUNT/COUNTD`) + arithmetic, `IF`/`ELSEIF`/`IIF`, comparisons + `AND`/`OR`/`NOT`, `ZN`/`IFNULL`/`ISNULL`; everything else → preserved-formula stub. |
| **Hidden join keys** (`<Base> (<Table>)`) | **Model relationship** | Direction inferred from real landed cardinality. |
| **Worksheet / Dashboard** | **Power BI report (PBIR)** | ✅ **Supported (preview)** — Tier-1 *structure* rebuilt (chart type, exact field bindings, position/layout, filters/parameters → slicers) into an openable, model-bound `.pbip`; visual *formatting* (colors, fonts, legends) is deferred to a later pass. |

### Decision Tree: Which storage mode?

```text
Tableau datasource
├── single relation that is a cross-engine join/union tree, OR a multi-connection table that
│     can't be routed to a specific upstream, OR no column metadata → FALL BACK: land-to-Delta + DirectLake
├── unknown/unmapped connector class                         → FALL BACK: land-to-Delta + DirectLake
├── flat file (Excel/CSV)                                    → Import (set file path)
├── extract enabled                                          → Import (snapshot); offer live DirectQuery if source supported
└── live relational (SQL Server/Azure SQL DB/Postgres/MySQL/Redshift) → DirectQuery (M fully emitted)
    ├── multiple named connections (each table → its own source) → DirectQuery rebuild + model relationships (DEFAULT, not a fallback)
    ├── Oracle / Snowflake / Databricks                       → DirectQuery mode; deploy-ready per-connector M emitted
    └── Teradata / BigQuery                                   → DirectQuery mode; flagged scaffold until a live navigator verifies the M
```

> **Default-direct policy.** Each table is rebuilt against its own source — **including** a federated
> datasource with several named connections, because Power BI relates the tables in the model layer.
> Land-to-Delta + DirectLake is an explicit **option**, auto-suggested only for the genuinely-undoable
> shapes above; when it triggers, `migrate_datasource` returns a `report["landing_plan"]` to act on.

> **Local-data POC (opt-in, no Fabric).** For a laptop demo — or a customer whose source connector
> has no live Power BI equivalent (S3 / MinIO, generic ODBC, Web Data Connector) and so would
> otherwise only get a `landing_plan` — pass `migrate_datasource(..., local_data=...)` to build a
> **clickable local Import model backed by real data in local CSV files**, with no Fabric workspace,
> lakehouse, or Azure Key Vault. `local_data` accepts a `{table: csv_path}` map, a directory of
> `*.csv`, a single `.csv`, a `.hyper`/`.tdsx`/`.twbx` file, or `True` (auto-extract the source's own
> `.hyper`). It reuses the proven `Csv.Document` Import generator (typed columns, calc→DAX, Date
> dimension, relationships, parameters) and reports under the additive `report["local_import"]` key.
> Auto-extracting a `.hyper` needs the optional `tableauhyperapi` wheel (`pip install tableauhyperapi`);
> bringing your own CSVs needs no extra dependency. **Limitation:** column types/renames line up only
> when the CSV headers match the `.tds` `<metadata-records>` remote names — otherwise the data still
> loads (headers promoted) but those columns stay untyped. When `local_data` is omitted the run is a
> byte-identical no-op.

See [storage-mode-selection.md](resources/storage-mode-selection.md) for the full policy and `scripts/storage_mode.py` for the executable version.

---

## Must / Prefer / Avoid

### MUST DO
- **Type every column from the source schema** (landed Delta for DirectLake, `.tds` `<metadata-records>` for Import/DirectQuery). Never deploy a model with inferred/guessed types — fall back instead.
- **Preserve every original Tableau formula** as a `TableauFormula` annotation on its measure, translated or not. This is the audit/repair safety net.
- **Default to a direct per-table rebuild** — each table binds to its own source, and Power BI relates multi-source tables in the model layer (so a federated, multi-connection datasource rebuilds direct, not via a lakehouse). Land-to-Delta + DirectLake is the explicit **option**, used only when a shape genuinely can't be rebuilt directly: a cross-engine join/union relation tree, a multi-connection table that can't be routed to a specific upstream, an unmapped connector, or missing column metadata. On that path `migrate_datasource` returns `report["landing_plan"]`.
- **Land data as Delta before generating a DirectLake model** — DirectLake binds to OneLake Delta, so the tables must exist first.
- **Deploy with the bundled `scripts/deploy_to_fabric.py`** (self-contained Fabric REST) so the migration finishes in Fabric without a peer-skill dependency; **or delegate deploy / bind / refresh / best-practice analysis** to `semantic-model-authoring` when that skill is available. Either way, do not hand-roll the `createItem` request inline.
- **Validate translated measures** by reconciling `ExecuteQuery` results against Tableau VDS values before declaring parity (see [validation-reconciliation.md](resources/validation-reconciliation.md)).

### PREFER
- **The lowest-friction storage mode per datasource** (extract→Import, live→DirectQuery) over forcing one mode across the estate.
- **`DIVIDE()` over `/`** and fully qualified `'Table'[Column]` references in generated DAX — the translator already emits these, aligning to standard Power BI DAX best practices (and `semantic-model-authoring`'s dax-guidelines when that peer skill is installed) so measures pass best-practice analysis.
- **DirectQuery native query with `[EnableFolding=true]`** for custom SQL so the query folds to the source.
- **A validation-gated LLM fallback** (opt-in) for stub measures — attempt a translation grounded by the preserved formula + DAX guidelines, accept it **only** if reconciliation passes, otherwise keep the inert stub.

### AVOID
- **Do not type Power BI columns from Tableau field roles or names** — use the physical source schema.
- **Do not claim a calculated field was translated** unless the deterministic translator produced DAX (or a gated LLM pass was reconciliation-verified). A stub is `= 0`, not a translation.
- **Do not emit a blind `(server, database)` call for Oracle/Teradata/Snowflake/BigQuery** — their signature/navigation differs; emit the verified per-connector M, or a flagged scaffold, but never a guessed 2-arg call.
- **Do not expand a Tableau join/union tree into independent Power BI tables** — that changes grain and breaks measures. Fall back.
- **Do not put credentials in the model, M code, `.tds` artifacts, or the migration report** — binding links IDs only; credentials are entered by the user on the connection.

---

## Examples

See the resource files for full walkthroughs. Key quick references:

**Calculated field → DAX (safe subset)**

```text
Tableau:  SUM([Profit]) / SUM([Sales])
DAX:      DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))
```

**Conditional + null handling → DAX (still inside the subset)**

```text
Tableau:  IF SUM([Sales]) > 0 THEN ZN(SUM([Profit])) / SUM([Sales]) ELSE 0 END
DAX:      IF(SUM('Orders'[Sales]) > 0, DIVIDE(COALESCE(SUM('Orders'[Profit]), 0), SUM('Orders'[Sales])), 0)
```

**Calculated field → preserved stub (outside the subset)**

```tmdl
measure 'Profit Bucket' = 0
    annotation TableauFormula = IF [Profit] > 0 THEN "Gain" ELSE "Loss" END
```

**Assisted translation → labeled suggestion → automatic validation-gated landing**

When a calc falls back to a stub, an **idiom registry** (`suggest_assisted_dax`) is consulted for
higher-level patterns whose faithful DAX is a *semantic* rewrite — e.g. **argmax-over-a-dimension**
("the city with the most sales", `IF [max city sales] = {FIXED [State],[City]:SUM([Sales])} THEN [City] END`).
A match is emitted as a **candidate** on the still-inert measure — never silently live before validation —
and surfaced in `report["assisted_suggestions"]`:

```tmdl
measure 'city with the most sales' = 0
    annotation TableauFormula = IF [Calculation_99] = {FIXED [State],[City]:SUM([Sales])} THEN [City] END
    annotation TranslationSuggestion = VAR __detail = CALCULATETABLE(ADDCOLUMNS(SUMMARIZE('Orders', 'Orders'[State], 'Orders'[City]), "@value", CALCULATE(SUM('Orders'[Sales]))), ALLEXCEPT('Orders', 'Orders'[State])) VAR __max = MAXX(__detail, [@value]) RETURN CONCATENATEX(FILTER(__detail, [@value] = __max), 'Orders'[City], ", ")
    annotation TranslationSuggestionPattern = argmax-dimension
```

Landing is **batch, not per-calc** — and once the user has authorized the second-compiler stage (see
*Post-Migration* step 3), it is **automatic** with no per-calc approval prompt: the second compiler
validates the `assisted_suggestions` list (the syntactic gate always; the reconciliation oracle when data
is landed), then re-runs with the validated subset to flip them into real measures in one pass (tagged
`TranslatedBy = assisted translation (human-approved)` — the historical provenance stamp for the assisted
tier). The deterministic safe-subset behavior is unchanged for everything else.

```python
from assemble_model import migrate_tds_to_semantic_model

# Pass 1 — see what the idiom registry can offer (nothing is live yet):
out = migrate_tds_to_semantic_model(tds_text, model_name="Superstore", calcs=calcs)
pending = out["report"]["assisted_suggestions"]   # [{measure, pattern, dax, confidence, caveats}, ...]

# The second compiler validates each candidate. Pass 2 — flip the validated ones into real measures:
approved = {s["measure"]: s["dax"] for s in pending}   # or filter by s["pattern"] == "argmax-dimension"
final = migrate_tds_to_semantic_model(tds_text, model_name="Superstore",
                                      calcs=calcs, approved_calc_dax=approved)
```

**Live SQL Server datasource → DirectQuery M partition**

```tmdl
expression Server = "myserver.database.windows.net" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]
expression Database = "Superstore" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]

table Orders
    column Sales
        dataType: double
        sourceColumn: Sales
    partition Orders = m
        mode: directQuery
        source =
            let
                Source = Sql.Database(#"Server", #"Database"),
                Data = Source{[Schema="dbo", Item="Orders"]}[Data]
            in
                Data
```

**Storage-mode decision (script)**

```python
from connection_to_m import parse_tds
from storage_mode import select_storage_mode

descriptor = parse_tds(open("datasource.tds", encoding="utf-8").read())
decision = select_storage_mode(descriptor)
# decision -> {'mode': 'DirectQuery', 'connector': 'Sql.Database', 'fully_supported': True, ...}
```

---

## Feature Parity Reference

Full matrix in [feature-parity.md](resources/feature-parity.md). Headline parity:

| Capability | Status |
|---|---|
| Datasource → semantic model (tables, typed columns) | ✅ High parity (types from source schema). |
| Relationship inference (hidden join keys) | ✅ Inferred from real landed cardinality (DirectLake path). |
| Calculated field → DAX | ⚠️ **Safe subset** — aggregations + arithmetic, `IF`/`ELSEIF`/`IIF`, comparisons + boolean logic, null handling (`ZN`/`IFNULL`/`ISNULL`), `CASE`→`SWITCH`, **FIXED / table-scoped LOD** (`CALCULATE(…, ALLEXCEPT/ALL)`), and row-level date/string functions (as calculated columns); `INCLUDE`/`EXCLUDE` LODs, non-additive re-aggregations, and view-context-dependent table calcs remain preserved stubs. |
| Storage mode / upstream connection | ✅ Auto-selected; `Sql.Database` family (SQL Server/Azure SQL DB/Postgres/MySQL/Redshift) plus Oracle, Snowflake, and Databricks emit deploy-ready per-connector M; Teradata/BigQuery are flagged scaffolds (live-navigator M not yet verified). |
| LOD expressions (FIXED/INCLUDE/EXCLUDE) | ⚠️ **FIXED translated** — single/multi-dim and table-scoped `{AGG(…)}` (`SUM`/`AVG`/`MIN`/`MAX`/`COUNT`; nested when each inner grain is a superset) → `CALCULATE(…, ALLEXCEPT/ALL)`, or `AGGX(SUMMARIZE(…), CALCULATE(…))` when re-aggregated. `INCLUDE`/`EXCLUDE`, non-additive `COUNTD` re-aggregation, and cross-table LODs → preserved stubs for manual/LLM completion. |
| View-only quick table calcs (running total, YTD (+growth), moving average, percentile, compound growth, percent difference, percent of total, year-over-year, difference) | ✅ **Rebuilt as Power BI Visual Calculations** on the report (`RUNNINGSUM`/`MOVINGAVERAGE`/`RANK`/`PREVIOUS`/`FIRST`/`ROWNUMBER`/`COLLAPSEALL` over the visual's own matrix axis), with the axis derived from the *view* and the original Tableau spec kept as provenance; a calc whose axis/offset/chain can't be pinned from the workbook routes to review, never a guess. |
| Worksheet / dashboard → Power BI report (PBIR) | ⚠️ **Supported (preview)** — Tier-1 *structure* (chart type, exact field bindings, position/layout, filters/parameters → slicers, default cross-filter, structural titles/axis names) rebuilt into an openable, model-bound `.pbip`; visual *formatting* (colors, fonts, legends, conditional formats) is not yet applied (deferred to a later pass). |
| Row-level security (wired user filters) | ⚠️ Translatable `USERNAME()` filters → TMDL `role`; group/compound logic fails closed (`FALSE()` + manual-review). |
| Parameters, sets, groups | ❌ Not migrated in v1 — flagged in the report. |

> **Key gaps**: calc coverage is a deterministic safe subset (not full); dashboard/worksheet rebuild is preview-level (Tier-1 *structure* only — chart type, exact field bindings, layout, slicers — with visual *formatting* such as colors/fonts/legends deferred to a later pass); parameters/sets/groups are **not rebuilt** as model objects (parameter-driven slicers are, however, surfaced on the rebuilt report). RLS is partially automated — wired `USERNAME()` filters become roles, while group/compound logic fails closed for deliberate review. The preserved `TableauFormula` annotations make every translated/stubbed measure auditable and repairable.

---

## Migration Gotchas — Quick Reference

Full guide in [migration-gotchas.md](resources/migration-gotchas.md).

| # | Flag ID | Issue | Blocks? | Resolution Summary |
|---|---|---|---|---|
| G1 | `TYPE_FROM_TABLEAU_METADATA` | Column typed from Tableau role/name instead of the physical schema → DirectLake bind fails | Yes | Type from landed Delta / `.tds` metadata; if absent, fall back. |
| G2 | `CALC_FALLBACK_STUB` | Calculated field outside the safe subset emitted as `= 0` | No | Expected — original formula preserved; repair manually or via gated LLM. |
| G3 | `JOIN_TREE_UNSUPPORTED` | Federated join/union tree treated as one logical table | Yes | Fall back to land-to-Delta + DirectLake; do not split into tables. |
| G4 | `CONNECTOR_NOT_EMITTED` | Teradata/BigQuery navigation not yet verified against a live navigator (Oracle/Snowflake/Databricks emit deploy-ready M) | Partial | Emit deploy-ready M where verified, else a flagged scaffold; never a guessed 2-arg call. |
| G5 | `NATIVE_QUERY_NO_FOLD` | Custom SQL native query won't fold in DirectQuery | Partial | Keep `[EnableFolding=true]`; if it still fails, switch that table to Import. |
| G6 | `CREDENTIALS_MANUAL` | Bind succeeds but refresh fails (no credentials) | Yes | User configures credentials on the connection; bind links IDs only. |
| G7 | `GATEWAY_REQUIRED` | DirectQuery to an on-premises source needs a data gateway | Yes | User sets up / selects a gateway for the connection. |

---

## Validation & Testing

See [validation-reconciliation.md](resources/validation-reconciliation.md). The migration is validated by:

1. **Structural** — model deploys and refreshes (DirectLake frames / Import loads / DirectQuery connects) without error.
2. **Translation self-tests** — `pytest` runs 900+ offline tests (translator subset + fallbacks + TMDL render + storage-mode policy + `.tds`/`.twb` parsing + workbook-datasource selection + landing-plan fallback + deploy payload builders).
3. **Value reconciliation (highest value)** — run each translated measure via `semantic-model-consumption` (`ExecuteQuery`) and compare to the Tableau VDS value pulled by the profiler. A measure is "verified" only when the numbers match.

---

## Security & Governance

See [security-governance.md](resources/security-governance.md). Key boundaries:

- **Credentials never leave the user.** Downloaded `.tds`/`.tdsx`/workbook artifacts are sensitive plaintext — do not commit them, embed them in the model/report, or include them in the migration report.
- **Binding links connection IDs only**; the user supplies credentials on the Fabric connection and sets up any on-prem gateway.
- **Never bind a source credential for the user — even via API.** A semantic model's TMDL has no password field; credentials live on a separate Fabric data connection the model binds to by ID. Setting them via REST still means transmitting the secret *and* requires the gateway's asymmetric (RSA-OAEP) credential flow — out of bounds. If a user pastes a secret in chat, do not write it anywhere and advise them to rotate it.
- **Least privilege** for the Tableau token (read/download scope) and the Fabric identity (`SemanticModel.ReadWrite.All` / `Item.ReadWrite.All`, model owner).

### After deploy: the credential-binding wall (expected)

A freshly deployed Import/DirectQuery model has **no credential bound**, so the first refresh fails with
`ModelRefreshFailed_CredentialsNotSpecified`. **This is success, not a bug** — the model is correct; the
human-owned bind is the only thing left. Hand off, then offer to re-trigger the refresh via API once bound:

1. Portal route — workspace → semantic model → **Settings → Data source credentials → Edit** (Basic auth + gateway if the source isn't publicly reachable).
2. **Licensing reality:** editing data-source credentials needs a **Pro / Fabric per-user** license — **F2 (or any capacity) alone is not enough**, and a trial may be expired. If the per-dataset Settings page is gated, try **Manage connections and gateways** (capacity-backed) to create a cloud connection and bind by ID, or have any Pro/Fabric-licensed colleague bind it once (it persists on the connection, not per-user).
3. Once bound by any route, re-run the refresh via the Power BI REST API (no portal needed for that step).

**Benign "needs refresh" triangles clear automatically.** A migrated model always carries two
self-contained Import calc tables — the auto `Date` table (`CALENDAR(...)`) and the `_Measures`
holder — alongside its (often DirectQuery) fact tables. A REST `createOrUpdate` deploy leaves those
Import tables *unprocessed*, so a composite model can open in the Fabric model view showing benign
limited-relationship / "column needs to be recalculated or refreshed" warning triangles until its
first refresh. `deploy_to_fabric.py` prevents this by running a **credential-free ProcessRecalc**
(`type: Calculate`) right after deploy: it processes only the calculated tables/columns, relationships
and hierarchies — **no `ProcessData`, so it needs no datasource credentials and never queries the
DirectQuery source** (verified even against an unreachable source) — mirroring how Power BI Desktop
recalculates a model when it is opened. This is **on by default**; pass `--no-recalc` to skip it. It
uses a Power BI token (`--use-az` or `POWERBI_TOKEN`) and is **best-effort and asynchronous** — it is
**started, not polled to completion** (a `202` is *accepted*, not *finished*), and if no token is available
the deploy still succeeds and simply logs that recalc was skipped.

**DirectQuery relationship cardinality (opt-in polish).** An authored Tableau object-graph join is
translated **many-to-many** by design — a wrong many-to-one on a non-unique target is rejected on
refresh and cancels the whole relationship batch (collateral-dropping the generated `Date` join). Once
the model is queryable (credentials bound + a first refresh done), `--upgrade-cardinality` reads the
deployed `relationships.tmdl` back, DAX-probes each many-to-many join's **target** column (`COUNTROWS`
vs `DISTINCTCOUNT` via `executeQueries`), and upgrades **only** the joins whose target is genuinely
unique to many-to-one — preserving each relationship's GUID and leaving any non-unique or unprobeable
join many-to-many. It is opt-in and best-effort (any doubt keeps the safe m:m), and it touches no
secret. `--finalize` runs the whole secret-free finish chain in one switch: bind (with `--gateway-id`)
→ recalc → refresh → upgrade-cardinality.

---

## Migration Report

See [migration-report.md](resources/migration-report.md). Every run produces an auditable report: per-datasource storage-mode decision + rationale, per-measure translation status (translated / stub + reason + preserved formula), **assisted-translation suggestions** (`report["assisted_suggestions"]` — labeled idiom matches the second compiler validates and lands automatically, never live before validation), inferred relationships, skipped tables, and the manual follow-ups (credentials, gateway, stub repair). This report is the trust artifact — it makes every gap explicit.

---

## Output: deploy to Fabric **or** write a local `.pbip`

The assemblers return `parts` (a TMDL `dict`). Three ways to land it — the agent should **not** improvise the layout (a prior pilot hand-rolled the `.pbip` and set the wrong `$schema`, which Power BI Desktop rejects):

- **Deploy to Fabric** — `fabric_definition_payload(parts)` → base64 parts for `scripts/deploy_to_fabric.py` (Fabric REST `createOrUpdate`).
- **Local semantic-model folder** — `write_model_folder(parts, "<Name>.SemanticModel")` writes a complete, valid **TMDL `.SemanticModel`** item (opens in Tabular Editor, git-reviewable, deployable). This alone is the model deliverable.
- **Openable Power BI project (`.pbip`)** — call the bundled helper; do **not** assemble the scaffold by hand:

```python
from assemble_model import write_local_pbip
write_local_pbip(parts, dest_dir, model_name="Superstore")   # → Superstore.pbip (double-click → Desktop)
```

It writes the proven layout with the **exact** schemas baked in (the part agents get wrong):

```
<Name>.pbip                  # $schema .../fabric/pbip/pbipProperties/1.0.0/schema.json ; artifacts→<Name>.Report
<Name>.SemanticModel/        # from write_model_folder(...) — the deliverable
<Name>.Report/               # thin one-page shell; definition.pbir datasetReference.byPath = ../<Name>.SemanticModel
```

The `.pbir` **`datasetReference.byPath`** is the report→model link. The default `.Report` is a thin
shell — the dataset is fully functional on its own — but the estate orchestrator now passes
`report_parts=` (from `twb_to_pbir`) to supply a **real rebuilt report** per workbook (see the note
below), and `project_name=` to name the project after the source asset. See
[semantic-model-rebuild.md](resources/semantic-model-rebuild.md).

> **Deploying the report to Fabric rebinds `byPath` → `byConnection`.** `byPath` is for opening the
> project locally; the Fabric REST API does **not** resolve it on deploy. `scripts/deploy_to_fabric.py`
> (`--pbip` / `--report-dir`, or `deploy_pbip` / `rebind_report_byConnection`) deploys the model first,
> then rewrites `definition.pbir` to a `byConnection` `semanticmodelid=<deployed-model-id>` reference
> before creating the `reports` item — fail-closed (report skipped, never half-bound) if there is no
> rebindable `definition.pbir`. See [public-api.md](resources/public-api.md) §3a.

> **Estate / local runs emit `.pbip` by default.** The one-button estate orchestrator
> (`scripts/migrate_estate.py`) writes an openable `pbip/<Name>/<Name>.pbip` for **every** migrated
> datasource — alongside (never replacing) the canonical `semantic_models/<Name>.SemanticModel/` — so a
> user can double-click straight into Power BI Desktop to explore and test each datasource. Pass
> `pbip=False` (CLI `--no-pbip`) to emit only the `semantic_models/` folders.

> **Workbooks emit an openable, model-bound `.pbip` too.** For every workbook with a rebuildable
> embedded datasource, the estate also writes a self-contained `pbip/<Workbook>/<Workbook>.pbip` — the
> Tier-1 rebuilt report (`twb_to_pbir`) bound *by path* to a sibling model rebuilt from the workbook's
> **own embedded datasource** — so the dashboard opens directly in Power BI Desktop. The per-workbook
> `viz_fidelity` list reports each visual as `rebuilt` or `warned`; anything that can't be faithfully
> bound (a lakehouse-fallback datasource, secondary datasources a single PBIR report can't bind) is
> recorded in `pbip_warnings` rather than mis-bound. The `semantic_models/` folders remain the
> canonical deploy target; the workbook `pbip/` is a self-contained local-open copy (by design).

---

## Post-Migration: What's Next

1. **Deploy** with the bundled `scripts/deploy_to_fabric.py` (self-contained Fabric REST), or **deploy & manage** with `semantic-model-authoring` when available (best-practice analysis, refresh, edits).
2. **Query & explore** with `semantic-model-consumption` and `fabriciq` (natural-language analysis over the migrated model).
3. **Offer the second compiler on every stubbed calc — an explicit, user-gated opt-in.** _Guard first: this step applies **only** when `report["summary"]["needs_review_total"] > 0`. If it is `0`, nothing is stubbed — there is nothing to offer; move on without re-reading the report._ When the deterministic pass leaves any calc stubbed (`report["summary"]["needs_review_total"] > 0`, also in `summary.md`'s **Next step** section and each `report["datasources"][n]["translation_handoff"]`), **present the stubbed calcs and ask the user whether to run the LLM-assisted second compiler — do not proceed on your own.** You must always *offer* it when there are stubs (never silently ship as if nothing can be done), but you *run* it **only** on an explicit yes/`GO`. If the user declines, the deterministic result ships as-is and every stub keeps its preserved `TableauFormula` — a complete, honest outcome (a stubbed calc is not a failed migration). **Once the user replies `GO`, run the full pass and auto-land every validated candidate (no per-calc approval):** work the Tier-1 loop per [second-compiler.md](resources/second-compiler.md): author the leanest *faithful* candidate DAX → `check_candidate_dax` (syntactic gate) → reconcile against the oracle when data is landed → **land every validated candidate automatically** via `approved_calc_dax` → redeploy. The **faithful-or-stub** charter still binds at the *landing* step: once authorized, the pass runs in full, but a calc with no faithful DAX form stays an inert stub (original `TableauFormula` preserved) — the validation gate, not the user prompt, is what prevents a guess going live.
   > _Ask, then run only on `GO` (do not proceed unprompted):_ "N of M calculations translated deterministically; K need review: `<Calc A>`, `<Calc B>`, … — run the LLM-assisted second compiler to attempt these? Reply `GO` to run it, or skip to ship the deterministic result as-is."
4. **Open the rebuilt reports (preview)** — each workbook with a rebuildable embedded datasource already ships as an openable `pbip/<Workbook>/<Workbook>.pbip` (Tier-1 *structure* — chart type, exact field bindings, layout, slicers — bound to the model). Open it in Power BI Desktop to review the rebuilt pages; check the per-workbook `viz_fidelity` for any `warned` visuals and apply visual *formatting* (colors, fonts, legends) by hand for now — that styling layer is a later pass.
5. **(Optional) Run the image oracle to settle ambiguous chart types** — for a workbook with non-standard / "hacky" views (a dual-axis pie that renders as a donut, a running-total Gantt that reads as a waterfall, an INDEX()/RANK() bump, a donut with a KPI floating in its hole), an opt-in **agent-driven vision pass** can confirm or correct each visual's *chart type* against the original Tableau rendering — **without ever touching field bindings**. It consumes the additive per-visual `candidate_records` `twb_to_pbir` already emits, resolves an offline-first image (caller-provided file → embedded `.twb`/`.twbx` thumbnail → none), and re-binds a visual's type **only** to a type in its candidate list. Follow the numbered runbook in [image-oracle.md](resources/image-oracle.md). Sheet swaps and field bindings stay deterministic; the Tier-1 report stands on its own if you skip this.

## Related skills

- [`tableau-datasource-profiler`](../tableau-datasource-profiler/SKILL.md) — run FIRST to inventory
  fields and assess migration readiness (calculated-field count, unsupported custom SQL, RLS/user
  references) before rebuilding the datasource here.
- [`tableau-mcp-landing-zone`](../tableau-mcp-landing-zone/SKILL.md) — after migrating, stand up the
  official Tableau MCP server so business users can natural-language-query Tableau from Copilot /
  Copilot Studio.
