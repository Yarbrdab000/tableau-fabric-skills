# LLM-optional adjudication — the "second matcher" playbook

The deterministic engine ([`comparison-methodology.md`](comparison-methodology.md)) is **Tier 0**:
it scores name / column / type / source overlap and emits the **authoritative** verdict for every
Tableau datasource. Like every structural matcher it is blind to **semantic** equivalence — and
that blindness is exactly where a customer's reuse-vs-rebuild count can go wrong:

- **Renamed columns.** A Fabric Lakehouse mirrors the source but snake-cases or re-friendlies the
  columns (`cust_id` → `CustomerKey`, `rev` → `Sales Amount`). The column Jaccard collapses even
  though it is the same data — a **false rebuild**.
- **Renamed asset.** The model was renamed on the way into Fabric, so the name signal drops while
  the columns still line up.
- **Obscured source.** A composite / DirectQuery model, a Lakehouse intermediary, or a referenced
  datasource hides the physical table, so the verdict rests only on name + columns — which could be
  a coincidental overlap of **generic column names** (`Date` / `Region` / `Sales`) describing
  *different* data — a **false match**.
- **Near tie.** Two Fabric models score within a hair of each other and the structure cannot pick.

These are precisely the cases a regex never resolves and a human/LLM resolves easily. So, mirroring
the `tableau-migration` skill's **second compiler**, Tier 0 hands the uncertain tail to a second
matcher.

> **The second matcher is the agent running this skill — not an embedded LLM API.** Tier 0 emits a
> structured, *categorized* adjudication request; the agent reads it, supplies the missing semantic
> judgement, and returns an **advisory** verdict. The deterministic tier/score **never change**, and
> **nothing is reclassified silently** — a default run produces zero agent verdicts.

The deterministic router lives in [`scripts/adjudicate.py`](../scripts/adjudicate.py)
(`build_adjudication`); the apply path is `apply_adjudication` in the same module.

---

## Where the two matchers meet

```text
           ┌──────────────────────── Tier 0 (deterministic) ────────────────────────┐
  inventories ─▶ compare_inventories() ─▶ best Fabric match + tier + 4 signals       │
           │                                                                         │
           │  confident (Exact, or a clean Strong)? ── yes ─▶ verdict stands as-is   │
           │                                                                         │
           └── not confident ─▶ adjudicate.build_adjudication(...)                   │
                                                  │  category + guidance + detail     │
                                                  ▼                                   │
           report["adjudication"] = { summary, needs_review, requests[] }   (additive)│
           └─────────────────────────────────────┬───────────────────────────────────┘
                                                  │
           ┌──────────────── Tier 1 (agent-as-second-matcher) ───────────────────────┐
           │  1. read request (category, guidance, Tableau columns, candidate detail) │
           │  2. judge SEMANTIC equivalence (align renamed columns, generic-name trap)│
           │  3. return a verdict: match | partial | no-match  + confidence + why     │
           │  4. apply_adjudication(result, verdicts) → agent_review + adjudicated_*   │
           └─────────────────────────────────────────────────────────────────────────┘
```

`report["adjudication"]` is **purely additive and always present** (empty lists when everything is
confident).

---

## The handoff request — what Tier 0 hands you

```jsonc
{
  "summary": { "total_reviewed": 2, "auto_confident": 4,
               "categories": { "renamed_columns_suspected": 1, "obscured_source": 1 } },
  "needs_review": [                       // concise list for the check-in prompt
    { "tableau_name": "Superstore", "tier": "Partial", "score": 0.46,
      "category": "renamed_columns_suspected", "deterministic_bucket": "partial" }
  ],
  "requests": [                           // one structured record per reviewed datasource
    {
      "tableau_name": "Superstore", "project": "Sales", "tableau_luid": "....",
      "category": "renamed_columns_suspected",
      "category_guidance": "The asset names and the column sets disagree …",
      "deterministic": { "tier": "Partial", "score": 0.46, "bucket": "partial",
                         "source_compared": false,
                         "best_fabric_id": "....", "best_fabric_name": "Superstore" },
      "tableau_columns": [ {"name": "cust_id", "type": "INTEGER"},
                           {"name": "rev", "type": "REAL"} ],
      "tableau_sources": [ {"connector": "snowflake", "database": "PROD", "table": "ORDERS"} ],
      "candidates": [                     // top-K Fabric models, each enriched for comparison
        { "fabric_id": "....", "fabric_name": "Superstore", "workspace": "Sales",
          "score": 0.46, "signals": {"name": 1.0, "column": 0.0, "type": 1.0, "source": null},
          "source_compared": false, "shared_column_count": 0,
          "columns": [ {"name": "CustomerKey", "type": "int64"},
                       {"name": "Sales Amount", "type": "double"} ],
          "tables": ["dim_customer"],
          "sources": [ {"connector": "databricks", "database": "lake", "table": "dim_customer"} ] }
      ]
    }
  ]
}
```

Everything you need to adjudicate is in the request — you do **not** re-pull either inventory.

---

## The category taxonomy — your routing map

Each category is a distinct playbook. Read `category`, then do the matching work. The full guidance
string ships in the request as `category_guidance`.

| Category | What it means | Judgement you must supply |
|---|---|---|
| `near_tie` | Two+ Fabric models scored within `~0.07` and at least one is a real contender. | Compare the close candidates' columns and business meaning; pick the true counterpart, or declare none. |
| `renamed_columns_suspected` | Name and column overlap **disagree** — names match but columns don't (renamed columns / lakehouse rebrand), or columns match but names don't (renamed asset). | Align columns **semantically** (`cust_id == CustomerKey`, `rev == Sales Amount`). If most columns map, it's a `match`. |
| `obscured_source` | Source obscured on one side (composite/DirectQuery, lakehouse mirror, referenced datasource); verdict rests on name+columns only. | Confirm it's the same data — and reject a coincidental overlap of **generic** column names that describe different data. |
| `borderline_band` | Best score lands in Partial/Weak — the zone where a real match is most often under-scored (Fabric added measures, split a star schema, renamed fields). | Decide: genuine partial overlap, or actually a strong match the structure missed? |
| `likely_rebuild` | Nothing overlaps structurally; Tier 0 calls it a rebuild. | Final sanity check for a semantic match before handing the datasource to `tableau-migration`. |

> The cheapest wins are `renamed_columns_suspected` and `obscured_source` — a quick column-by-column
> semantic alignment usually settles them.

---

## The output contract — what you produce per request

For each request you adjudicate, return a **review record**:

```jsonc
{ "tableau_luid": "....",            // or "tableau_name"
  "verdict": "match",                // match | partial | no-match  (synonyms accepted)
  "fabric_id": "....",               // which candidate you matched (defaults to the best)
  "confidence": "high",              // high | medium | low — be honest about residual ambiguity
  "rationale": "cust_id==CustomerKey, rev==Sales Amount; same Snowflake ORDERS mirrored in the lakehouse" }
```

- **`verdict`** maps to a rollup bucket: `match` → already-exists, `partial` → partial, `no-match`
  → rebuild.
- **`rationale`** is the column/source alignment you used — it's what lets a human trust the call.
- **`confidence`** carries your uncertainty; a `low`-confidence `match` is still worth surfacing,
  flagged.

Hand the records back via `apply_adjudication(result, {"reviews": [ … ]})` (or `--apply-adjudication
verdicts.json` on `compare_estate.py`). It attaches an `agent_review` to each reviewed match and
produces an `adjudicated_summary` rollup with the delta versus the deterministic count.

---

## Hard safety invariants

1. **Deterministic verdict is untouched.** `apply_adjudication` only *adds* `agent_review` and an
   `adjudicated_summary`; it never rewrites `tier` / `score` / `bucket` or the deterministic
   `summary`. The two rollups sit side by side so the customer sees structural vs. semantic.
2. **A default run adds ZERO agent verdicts.** The adjudication packet is surfaced; verdicts are
   applied only when you explicitly pass them.
3. **Evidence, not vibes.** Every `match` / `partial` verdict must carry a column/source rationale.
   A guess with no alignment is a `low`-confidence note, not a reclassification.
4. **Advisory, always reviewed.** The report is a ranked worklist a human confirms before retiring a
   Tableau datasource — the second matcher sharpens the worklist, it does not automate the decision.

---

## Worked example — renamed columns across a lakehouse boundary

```text
request.category = "renamed_columns_suspected"
deterministic    = tier Partial, score 0.46, source_compared false
tableau_columns  = [cust_id:INTEGER, rev:REAL, ord_dt:DATE]
candidate.columns= [CustomerKey:int64, Sales Amount:double, Order Date:dateTime, Margin %:double]
candidate.sources= [{databricks, lake, dim_customer}]      (Tableau read Snowflake ORDERS directly)
```

1. **Align semantically:** `cust_id ≈ CustomerKey`, `rev ≈ Sales Amount`, `ord_dt ≈ Order Date`.
   All three Tableau columns map; the extra `Margin %` is a Fabric-added measure.
2. **Source:** Tableau connects to Snowflake `ORDERS` directly; the candidate reads a Databricks
   lakehouse `dim_customer` that mirrors it — consistent with the lakehouse-intermediary pattern, so
   the obscured source is *expected*, not disqualifying.
3. **Verdict:** `match`, confidence `high`, rationale = the alignment above. → `apply_adjudication`
   moves this datasource from the deterministic `partial` bucket into `already_exists` in the
   `adjudicated_summary` (the deterministic Partial verdict stays on the row for the audit trail).
