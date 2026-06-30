# Security & Governance

The security boundaries this skill respects, and the ones that stay with the user. The guiding principle:
**the skill emits artifacts; the human owns secrets and access.**

---

## Sensitive artifacts

Downloaded Tableau files are **plaintext** and can embed server names, database names, and sometimes
connection details:

| Artifact | From | Handling |
|---|---|---|
| `.tds` / `.tdsx` | Download Data Source | Sensitive; git-ignored; never embed in the model or the report |
| `.twb` / `.twbx` | Download Workbook (v2) | Sensitive; same handling |
| `.hyper` | Extract | Sensitive; contains real data |

Rules:

- **Never commit** these to the repo — they are in `.gitignore`. Treat any accidental staging as an incident.
- **Never paste** raw artifact contents into the migration report or chat output.
- The parsed **descriptor is credential-free by design** (`parse_tds` extracts only structural metadata:
  connector class, server, database name, relations, typed columns). Prefer passing the descriptor, not the
  raw `.tds`, to downstream steps.

---

## Credentials are a manual boundary

The skill emits connection **parameters** and the structured **bind inputs** (`connection_details_for_bind`),
but it **never reads, stores, or enters
credentials**. The user supplies them when creating/binding the Fabric Data Connection.

> On any credential error during bind or refresh, **stop** and have the user configure the connection. Do
> not retry with guessed credentials and do not prompt for secrets to put into a file.

---

## Tokens

| Token | Audience | Notes |
|---|---|---|
| Tableau REST/Metadata/VDS | Tableau Server / Cloud | From a PAT (name + secret) or Connected-App JWT; keep out of all output |
| Fabric REST | `https://api.fabric.microsoft.com` | Acquire via `az account get-access-token`, or the bundled `scripts/deploy_to_fabric.py` (`--use-az` / `--token` / `FABRIC_TOKEN`) |

- Acquire tokens at the start (orchestrator Phase 0), keep them in memory, and never write them to disk or
  the report.
- Prefer the standard auth/token-audience patterns in `common/COMMON-CORE.md` over bespoke per-run config.

---

## Local secure prompt (no Azure Key Vault)

Azure Key Vault is the **default** way the live pull obtains the Tableau PAT (or Connected-App) secret, but
it is **not required**. The orchestrator asks **D6 — "How would you like me to access the Tableau
credentials: (A) Azure Key Vault, or (B) a local secure terminal prompt?"** so the choice is explicit, never
a silent fallback.

**D6=B — Local Secure Prompt.** Run `fetch_tds.py --prompt-secret` (or simply leave `TABLEAU_PAT_VALUE`
unset). The script asks for the secret at a **hidden** `getpass` prompt in that terminal, exchanges it for a
short-lived session token, and clears it from the process environment in a `finally` block. Guarantees:

- The secret is held **in memory only** for the duration of the call — it is **never** echoed back, written
  to disk (`.env`, logs), placed in the migration report, or shown in chat.
- An **empty entry is rejected** (fail fast) rather than silently attempting an anonymous sign-in.
- It is reached only when a console is attached; `--no-prompt` forbids it for unattended/CI runs (those must
  supply the secret via a flag or env var).

This is the masked layer of the dependency-free resolver in `scripts/credential_resolver.py`, which also
accepts — in order — an explicit value, the `TABLEAU_PAT_VALUE` environment variable, a git-ignored `.env`
entry, or an OS keyring secret (Windows Credential Manager / macOS Keychain / Secret Service, via the
optional `pip install keyring`) before it prompts.

> **Customer-facing wording.** *"If you do not have Azure Key Vault, choose **Local Secure Prompt** mode: I
> will open a hidden prompt in your terminal for your Tableau Personal Access Token secret. Type it directly
> into the terminal — do not paste it into chat. The value stays in memory only for the sign-in, is never
> written to disk or any report, and is cleared as soon as the session token is obtained."*

The masked prompt covers only **how the secret is entered**; the Fabric-side credential boundary below is
unchanged — the skill still never enters database credentials for the bound connection.

---

## Gateways (on-premises sources)

DirectQuery against an on-premises source requires an **on-premises data gateway** that the user selects or
sets up. The skill flags this in `decision["manual_followups"]`; it cannot provision a gateway.

---

## Row-level security and governance objects

RLS roles **are** rebuilt where it is provably safe: a user filter wired as a data-source filter with a
`[Field] = USERNAME()` shape becomes a TMDL `role` (`USERNAME()` → `USERPRINCIPALNAME()`). Anything without a
safe deterministic DAX equivalent (`ISMEMBEROF` group logic, `USERDOMAIN()`, compound expressions, an
unresolvable field) **fails closed** — `FALSE()` on every table plus a `RequiresManualReview` annotation that
preserves the original formula — and an unwired user-function calc is reported, never turned into a role. The
principle is unchanged: re-creating RLS incorrectly is worse than not creating it, so the boundary is either
provably correct or explicitly handed to a human. Object-level security, perspectives, and sensitivity labels
remain **not migrated** and are reported. See [model-enrichment.md](model-enrichment.md).

---

## Least privilege

- Use a Tableau identity scoped to the datasources being migrated.
- Use a Fabric identity scoped to the **target workspace** only.
- Nothing in the skill needs tenant-admin rights; if a step seems to, re-check the scope rather than
  escalating.

---

## What stays manual (summary)

Entering connection **credentials**, selecting/standing up an on-prem **gateway**, completing any
**fail-closed RLS** roles (group logic / non-deterministic filters) and re-applying other governance
objects, and reviewing **custom-SQL folding** before refresh. Everything else — model, translatable RLS,
parameters, bind inputs — the skill produces.
