# Tableau Datasource Profiler

A read-only Copilot CLI skill that profiles a published Tableau datasource and reports its
fields, types, calculated-field formulas, lineage, and (optionally) value-level statistics.

Built directly on Tableau's REST + Metadata + VizQL Data Service APIs — **no
`tableauserverclient`**, so it works against both **Tableau Cloud** and **Tableau Server**.

See [`SKILL.md`](./SKILL.md) for full setup, usage, and agent guidance. Quick version:

```bash
pip install -r requirements.txt

export TABLEAU_SERVER="https://10ax.online.tableau.com"
export TABLEAU_SITE="your_site_content_url"
export TABLEAU_PAT_NAME="your_pat_name"
export TABLEAU_PAT_VALUE="your_pat_secret"

python scripts/profile_datasource.py --datasource-name "Superstore"            # schema only
python scripts/profile_datasource.py --datasource-name "Superstore" --with-stats # + value stats
python scripts/profile_datasource.py --datasource-name "Superstore" --dry-run   # show requests only
```

### Auth: PAT (default) or Connected App JWT

PAT is the default. Alternatively, sign in with a **Connected App (Direct Trust)** JWT via
`--auth jwt`. This avoids PAT rotation and lets you **act as a specific user** (`sub` claim) —
use a Site Admin to bypass RLS and get complete value stats. The JWT is built and signed
(HS256) with the standard library, so there is **no extra dependency**.

Set up in Tableau Cloud (**Settings → Connected Apps → New Connected App → Direct Trust**):
create the app, generate and **enable** a secret, and grant these JWT access scopes:
`tableau:content:read` and `tableau:viz_data_service:read` (the latter is needed for
`--with-stats`). Then:

```bash
export TABLEAU_SERVER="https://10ax.online.tableau.com"
export TABLEAU_SITE="your_site_content_url"
export TABLEAU_CONNECTED_APP_CLIENT_ID="<connected app Client ID>"
export TABLEAU_CONNECTED_APP_SECRET_ID="<Secret ID>"
export TABLEAU_CONNECTED_APP_SECRET_VALUE="<Secret Value>"
export TABLEAU_JWT_USERNAME="admin@example.com"   # user to act as (Site Admin for full data)
# optional: override scopes (space/comma separated)
# export TABLEAU_JWT_SCOPES="tableau:content:read tableau:viz_data_service:read"

python scripts/profile_datasource.py --datasource-name "Superstore" --auth jwt --with-stats
```

> The JWT structure is validated against Tableau's official sign-in reference and was
> **live-tested** against a real Tableau Cloud connected app (sign-in + Metadata API + VDS
> authorization all confirmed).

## Two tools

- **`profile_datasource.py`** — schema + optional value statistics (below).
- **`query_datasource.py`** — answers business questions by running a structured **VizQL Data
  Service query** (aggregations, filters, sorting, top-N). An agent turns a natural-language
  question into the query object; the tool executes it and returns rows (md or json). Example:

  ```bash
  python scripts/query_datasource.py --datasource-name "Superstore" --query-json \
    '{"fields":[{"fieldCaption":"Region"},{"fieldCaption":"Sales","function":"SUM"}]}'
  ```

  See [`SKILL.md`](./SKILL.md) and the script header for the full field/filter schema.

## What the profiler gives you

- **Schema profile (default, no rate limit):** per-field role, data type, hidden
  flag, folder, description, calculated-field formulas, and lineage; plus datasource-level
  migration signals (`containsUnsupportedCustomSql`, calculated-field count, `hasUserReference`).
- **Value stats (`--with-stats`, optional):** approximate row count, null rates, cardinality,
  numeric ranges, and date ranges via the VizQL Data Service — sent one aggregate function per
  query (VDS forbids repeating a field in a query) and batched to respect the 100 calls/hour
  per-Creator limit. Bin/group/set and other non-aggregatable fields are skipped robustly
  (batches that 400 or return grouped rows are split and retried), and stats degrade to
  schema-only with a note on a 429. Requires Tableau 2025.1+ with VDS enabled. Note: value
  stats reflect only rows the PAT user can see under RLS — use a Site Admin PAT for full data.

## How it fits this collection

This profiler is the **read-only entry point** to the same Tableau APIs the other skills in this
collection build on:

- **`tableau-mcp-landing-zone`** hosts these capabilities as a live MCP server in Azure so business
  users query Tableau from Microsoft Copilot — using the *same* Connected App. Profiling a datasource
  first is a quick way to confirm that Connected App + scopes work before standing up the server.
- **`tableau-migration`** consumes this profile's migration signals (calculated-field count,
  unsupported custom SQL, RLS / user references) when rebuilding a datasource as a Fabric / Power BI
  semantic model.
