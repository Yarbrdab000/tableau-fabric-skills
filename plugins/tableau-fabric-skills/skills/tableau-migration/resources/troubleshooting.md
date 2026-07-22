# Troubleshooting — "I need help"

A **guided menu** to run when the user is stuck: they said *"I need help"* / *"it's not working"* /
*"troubleshoot this"*, or they described a snag. This is the **user-facing** recovery guide — the
practical "the tool can't do X, now what" flow. (The deep, agent-facing failure catalog is
[migration-gotchas.md](migration-gotchas.md); this guide *routes into* it.)

> **How to use it.** Present the **top-level menu below verbatim** and ask the user to reply with a
> number. Then jump to that section, work the symptom table top-to-bottom (**Do this now**), and only
> open the linked deep resource if the quick fix doesn't clear it. Ask one clarifying question at a
> time; never guess a credential, a path, or a fix. If nothing fits, use **§8 Not sure**.

```
What are you running into? Reply with a number and I'll walk you through it:

  1) The skill won't load or run          (install, /skills list empty, wrong Python, out of date)
  2) Tableau sign-in / credentials        (PAT not working, no Key Vault, no credentials at all)
  3) Can't pull from Tableau (online)     (server/site, a datasource or workbook not found on Server/Cloud)
  4) A local file or its data isn't found (tool can't find my .tds/.twb/.twbx, or a local dashboard's
                                           data/extract/flat-file is missing)
  5) A step errored or the run stalled    (the happy path isn't completing; is WARN a failure?)
  6) Deploy to Fabric failed              (az login, workspace/capacity, refresh / credential wall)
  7) The output looks wrong               (.pbip "tiny/broken", calcs = 0, a visual missing, numbers off)
  8) Not sure / none of these             (describe it and I'll route you)
```

---

## 1) The skill won't load or run

| Symptom | Likely cause | Do this now |
|---|---|---|
| `/skills list` is empty, or the skill never triggers | The skill folder was **copied** into a skills dir — current clients don't auto-scan that | Register the **plugin** instead, then start a **new** session: `/plugin marketplace add Yarbrdab000/tableau-fabric-skills` then `/plugin install tableau-fabric-skills@tableau-collection`. Verify with `/plugin list` (expect `tableau-fabric-skills`) and `/skills list`. See `INSTALL.md` at the repo root. |
| `/plugin list` doesn't show `tableau-fabric-skills` after install | The install ran in the **current** session; plugins load at session start | Start a brand-new session and re-check `/plugin list`. |
| A bundled script errors with a syntax / "pytest not found" / f-string error | Wrong Python — a bare `py` resolves to 3.14 here and lacks pytest | Run every script and the tests with **`py -3.11`** (e.g. `py -3.11 scripts\fetch_tds.py …`). |
| A resource or script parses with garbled first characters | UTF-8 **BOM** on the file | Read it as `utf-8-sig` — Tableau (and some of these files) write a BOM. |
| "This worked in a different version" / seems out of date | The installed copy is behind `main` | Follow [self-update.md](self-update.md) — it compares the installed `VERSION` to the remote and only reinstalls when `main` is newer. |

---

## 2) Tableau sign-in / credentials  ·  *headline*

**First branch the user:** *is the PAT being rejected, or can you not supply credentials at all?*

### 2a — The PAT isn't working / sign-in is rejected
| Symptom | Likely cause | Do this now |
|---|---|---|
| Sign-in fails / "invalid credentials" with a PAT | Only the token **name** or only the **secret** was supplied — sign-in needs **both** | Provide **both** `TABLEAU_PAT_NAME` (the token's name — *not* a secret) **and** `TABLEAU_PAT_VALUE` (the secret). An empty secret is rejected on purpose (no anonymous sign-in). |
| Sign-in fails and the PAT is definitely correct | Tableau PATs **expire** — after a period of inactivity, or at the tenant lifetime limit — and are one-secret-per-name | **Regenerate** the PAT in Tableau (*My Account Settings → Personal Access Tokens*), then update the secret you pass in. |
| "site not found" / signs in but sees nothing | Wrong `--site` (it's the site's **content-URL** name, not its display name; Tableau Server's default site is blank) or wrong `--server` host | Confirm the exact `--server` host and `--site` content-URL from the browser address bar. |
| You deliberately want Connected-App JWT and it fails | JWT (D5=B) needs the Connected App **client id + secret**, on purpose | Only use JWT if you chose **D5=B**; otherwise stay on **PAT (D5=A)**. Never silently switch auth modes. |

Deeper: [security-governance.md](security-governance.md) (**Tokens**).

### 2b — You don't have Azure Key Vault
Key Vault is only the **default** way to fetch the secret — it is **not required**.

| Situation | Do this now |
|---|---|
| No Key Vault, run is agent-driven | Use **D6=B** (a non-interactive local secret). Put `TABLEAU_PAT_NAME` + `TABLEAU_PAT_VALUE` in a **git-ignored `.env`** (see `.env.example`), then pass `--env-file .env --no-prompt`. The file survives across the agent's fresh per-command processes; a `$env:` variable would not. |
| You prefer the OS secret store | Store it once, then pass `--keyring-service <name>` (needs `pip install keyring`). |

Deeper: [security-governance.md](security-governance.md) (**Local credentials without Azure Key Vault**).

### 2c — You can't get online credentials **at all**
There's an escape hatch: **you don't need Tableau credentials to migrate.**

| Situation | Do this now |
|---|---|
| No PAT, no Connected App, no access to the Tableau REST API | Switch **D1 → B (Local files)**. Export the `.tds` / `.tdsx` / `.twb` / `.twbx` from Tableau yourself (Server/Desktop → *Download*), drop them in `.\in`, and migrate those. The **live pull and every Tableau credential are skipped entirely** — the whole flow runs offline from the files. |

Deeper: the **D1** option in the Decision Menu (`SKILL.md`).

### 2d — The tool tried to prompt me / it hung / I couldn't paste
| Symptom | Likely cause | Do this now |
|---|---|---|
| A secret prompt appeared in a process you can't see, or blocked paste | An **agent-driven** run has no shared interactive TTY, so the hidden `getpass` prompt (**D6=C**) is unanswerable | Never use **D6=C** when an agent drives the run. Resolve the secret from a file/keyring (**2b**) and add `--no-prompt` so it can never fall back to a prompt. |

Deeper: [security-governance.md](security-governance.md) (**⚠️ Agent-driven / non-interactive runs**).

---

## 3) Can't pull from Tableau Server/Cloud (online)

| Symptom | Likely cause | Do this now |
|---|---|---|
| "datasource/workbook not found" on a live pull | Name mismatch, or it's on a different site/project | Use the **exact** published name; confirm `--server`/`--site`. Sign-in first (see **§2**) — a "not found" often masks an auth/site problem. |
| A pulled **workbook** rebuilds to an **empty** report | The workbook connects to a **published** datasource; only a `sqlproxy` stub travels with it | The STEP 1.5 scan names that datasource **before** building — fetch it by name **into scope** and re-scan so it migrates first, then the report binds to it. |
| Not sure whether it's embedded or published | — | Let the **STEP 1.5 scan** classify it (you never inspect XML). It tells you embedded vs published; a published one just needs its data in scope. |

Deeper: [connection-binding.md](connection-binding.md); the STEP 1.5 scan in `SKILL.md`.

---

## 4) A local file or its data isn't found

> When you **attach** a file, the harness writes it to disk and gives you its **absolute path in the
> message** — *that attachment IS the input*. Copy that exact path into `.\in` and move on. **Never**
> re-type a path the user already gave, and **never** run a recursive/disk-wide "search files" scan to
> "find" it — a broad scan grabs a **stale duplicate** from another folder (OneDrive, an old run).

| Symptom | Likely cause | Do this now |
|---|---|---|
| "Can't find my `.tds` / `.twb` / `.twbx`" | The pinned input path is wrong, or the file was never placed in scope | Use the **absolute path from the attachment message**, or place the file under `.\in` and name it in **D2** scope. Confirm the exact filename (spaces, extension) — don't scan the disk for it. |
| `.tdsx` / `.twbx` "won't open" or "can't find data" | It's a **ZIP**, not XML — the datasource **and any extract** live *inside* it | Nothing to fix: the parser unzips it (the `.tds` is at the root or under `Data/`). If you hand-extract, unzip first. |
| A local **dashboard rebuilds empty / "no data"** | A bare `.twb` connected to a **published** datasource carries only a `sqlproxy` stub — the data isn't local | Use the **`.twbx`** (it packages the extract), or bring the datasource into scope (**§3**). A live-connection `.tds` has **no local data by design**. |
| A **flat-file** (Excel/CSV) model has no data / no path | The `.tds` doesn't carry the workbook-relative file path | Supply the file path on the **M partition** so the model can find the source. |
| A `.hyper` extract "can't be found" | The extract must be **in scope** — inside the `.twbx`/`.tdsx` or provided alongside | Migrate the packaged `.twbx`/`.tdsx` (which contains the `.hyper`), or provide the extract. |

Deeper: [migration-gotchas.md](migration-gotchas.md) (**Parsing the `.tds`**, **Storage mode → flat file**); the "never search the filesystem / attachment path" gate in `SKILL.md`.

---

## 5) A step errored or the run stalled

| Symptom | Likely cause | Do this now |
|---|---|---|
| A STEP checkpoint failed | This is a **gated runbook** — a failed checkpoint is a **STOP-and-ask**, not a self-fix | Read `.\out\summary.md`, report what failed, and ask the user. Do **not** self-diagnose, re-fetch, or re-run to "fix" it. |
| The run finished **WARN** / "degraded" | **WARN is the normal, healthy finish** — some calcs need review or a visual was approximated | Read the **"Next step"** sections in `summary.md` and report the gaps. A shortfall is a STOP-and-ask, **never** a hand-rebuild or re-run. |
| `.tds` won't parse | UTF-8 BOM, or a `.tdsx`/`.twbx` (a zip) was passed as XML | Open with `encoding="utf-8-sig"`; unzip a `.tdsx`/`.twbx` first (see **§4**). |
| `storage mode = None`, or a connector emits a "scaffold" | Expected for a join/union tree, >1 named connection, an unmapped connector, or no column metadata | Route to land-to-Delta + DirectLake; review the scaffolded M before refresh. The mode is right; the navigation needs a glance. |

Deeper: [migration-gotchas.md](migration-gotchas.md) (**Parsing**, **Storage mode**, **Deploy & validate**).

---

## 6) Deploy to Fabric failed

| Symptom | Likely cause | Do this now |
|---|---|---|
| Deploy can't get a token / "az" errors | No Fabric token in the environment | Sign in with `az login`, then let `scripts/deploy_to_fabric.py --use-az` acquire the token (or pass `--token` / `FABRIC_TOKEN`). |
| "unauthorized" / workspace or capacity error | The identity isn't scoped to the **target workspace**, or the capacity is paused | Use a Fabric identity scoped to that workspace only (no tenant-admin needed); confirm the capacity is running. |
| A model of the same name already exists | Conflict policy | Honor **D4** — overwrite / skip / stop-and-ask (default **stop**). Don't hand-roll `createItem`; delegate deploy to the bundled script / `semantic-model-authoring`. |
| Deployed, but tables show **warning triangles** or refresh fails on credentials | The **credential-binding wall** — the model deploys with IDs only; the connection still needs credentials | Have the user **configure credentials** on the Fabric connection; never enter database credentials for them. A benign "needs refresh" triangle clears after a credential-free **recalc** (see the credential-wall section in `SKILL.md`). |
| DirectQuery to an on-prem source fails | No **on-premises data gateway** | The user selects/sets up a gateway; the skill flags it in `manual_followups` but can't provision one. |

Deeper: [migration-gotchas.md](migration-gotchas.md) (**Deploy & validate**); the credential-wall section in `SKILL.md`.

---

## 7) The output looks wrong

| Symptom | Likely cause | Do this now |
|---|---|---|
| The `.pbip` is "only ~300 bytes / a tiny JSON stub" | That is **exactly** what a correct `.pbip` is — a JSON **pointer** to its sibling folders | **Nothing is wrong. Do NOT zip, repackage, or "fix" it.** Double-click it in Power BI Desktop. Verify with `py -3.11 scripts/deploy_to_fabric.py --verify-pbip <bundle-or-.pbip>`. |
| Power BI: `Unable to translate bytes [XX] at index N` on open | The `.pbip` was overwritten with a **ZIP** (a `PK..` header fed to a JSON parser) | Someone zipped the pointer. **Restore it** (re-run the migration). Every *sibling* format (`.pbix`/`.twbx`/`.tdsx`) is a zip — the `.pbip` is **not**. |
| A calculated field shows **`= 0`** | A calc outside the safe subset was emitted as an inert stub (its original formula is preserved) | Offer the **second-compiler** pass to author the DAX ([second-compiler.md](second-compiler.md)); don't hand back silent `= 0` stubs. |
| A visual is missing / a placeholder | A disclosed `warned` rebuild (unsupported visual or no usable bindings) | It's reported, not lost — read the worksheet's `viz_fidelity` / warnings row; offer the assisted dashboard-audit tier. |
| A number doesn't match Tableau | Different **filter context** on the two sides, or cross-engine rounding | Match the filter context first; compare with a **relative epsilon**, not exact equality. A genuine gap is a real mismatch to investigate. |
| Edited a `.tmdl`/`.m` file but Desktop still runs the old query | Desktop compiles the `.pbip` **once at open** and doesn't watch files | **Close and reopen** the `.pbip` to force a fresh read from disk. |

Deeper: [migration-gotchas.md](migration-gotchas.md) (**the `.pbip` is a pointer**, **Editing the output**), [second-compiler.md](second-compiler.md), [validation-reconciliation.md](validation-reconciliation.md).

---

## 8) Not sure / none of these

Ask 1–2 clarifying questions to place the problem, then route to the section above. If it's a **finished
run**, the fastest ground truth is the run's own report: read `.\out\summary.md` (human) and
`.\out\report.json` (machine) — they name every gap, stub, warning, and "Next step" already. If it's a
genuine failure, **STOP and ask the user** with what `summary.md` shows; never self-diagnose past a failed
checkpoint or re-run to "fix" a disclosed shortfall.
