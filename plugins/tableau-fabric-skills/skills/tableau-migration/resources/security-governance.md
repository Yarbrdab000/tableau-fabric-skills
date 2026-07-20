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

## Local credentials without Azure Key Vault

Azure Key Vault is the **default** way the live pull obtains the Tableau PAT (or Connected-App) secret, but
it is **not required**. The orchestrator asks **D6 — "How would you like me to access the Tableau
credentials?"** with three explicit options (never a silent fallback):

- **(A) Azure Key Vault** — the default when a vault is available.
- **(B) A non-interactive local secret — a git-ignored `.env` file or an OS keyring.** **This is the
  recommended no-Key-Vault path, and the *required* one whenever an agent is driving the run
  non-interactively.**
- **(C) An interactive hidden terminal prompt** — only for a **genuine human-attended terminal**.

> **⚠️ Agent-driven / non-interactive runs must use (B), not (C).** When an agent (e.g. Copilot CLI) drives
> the migration, every command runs in a **fresh, isolated process** with no shared interactive TTY: a hidden
> `getpass` prompt appears in a background process the user cannot see or answer, and a terminal-canvas prompt
> **blocks paste**, forcing an error-prone character-by-character entry of a long token. Do **not** choose the
> interactive prompt in that environment — resolve the secret from a file or keyring instead, which needs no
> live prompt.

**D6=B — non-interactive local secret (recommended).** Put the secret in one of the resolver's
non-interactive layers and point `fetch_tds.py` at it — no prompt is ever shown:

- **Git-ignored `.env` file (simplest).** Create a `.env` (see [`.env.example`](../.env.example)) with the
  token **name and secret**, then pass `--env-file`:
  ```powershell
  # .env  (never committed — .gitignore already excludes .env / .env.*)
  # TABLEAU_PAT_NAME=Migration-PAT
  # TABLEAU_PAT_VALUE=<the token secret>
  py -3.11 scripts/fetch_tds.py --server <host> --site <site> `
      --datasource-name "<name>" --env-file .env --no-prompt --out .\pulled
  ```
  The file persists on disk across the agent's fresh per-command processes (a `$env:` variable set in one
  command would **not** survive to the next), so this is the robust agent-compatible choice. Add `--no-prompt`
  to guarantee it never falls back to a prompt.
- **OS keyring** (Windows Credential Manager / macOS Keychain / freedesktop Secret Service; `pip install
  keyring`): store the secret once, then pass `--keyring-service <name>` (and optionally
  `--keyring-username <user>`).

The token **name** (`TABLEAU_PAT_NAME` / `--pat-name`) is **not** a secret and is needed alongside the secret;
signing in requires both. The secret **value** is held in memory only for the sign-in — it is **never** echoed,
written to disk (`.env` files are read, never written), placed in the migration report, or shown in chat, and
an **empty entry is rejected** (fail fast) rather than attempting an anonymous sign-in.

**D6=C — interactive hidden prompt (human-attended terminal only).** Run `fetch_tds.py --prompt-secret` (or
leave every secret source unset). The script asks for the secret at a **hidden** `getpass` prompt in *that*
terminal, exchanges it for a short-lived session token, and clears it in a `finally` block. It is reached only
when a console is attached; `--no-prompt` forbids it for unattended/CI/agent runs.

These are the layers of the dependency-free resolver in `scripts/credential_resolver.py`, which the CLI now
wires end-to-end — an explicit flag, the `TABLEAU_PAT_VALUE` (or `TABLEAU_CONNECTED_APP_SECRET_VALUE`)
environment variable, a git-ignored `.env` entry (`--env-file`), an OS keyring secret (`--keyring-service`),
and only then the masked prompt.

> **Customer-facing wording.** *"If you do not have Azure Key Vault, put your Tableau Personal Access Token
> name and secret in a local `.env` file (I never commit it) and I will read it directly — no prompt, and
> nothing is pasted into chat. The secret stays in memory only for the sign-in, is never written to any
> report, and is discarded as soon as the session token is obtained. A hidden terminal prompt is available
> too, but only if you are running in a real interactive terminal yourself."*

The credential choice covers only **how the secret is entered**; the Fabric-side credential boundary below is
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
