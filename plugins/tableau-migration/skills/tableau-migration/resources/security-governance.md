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
