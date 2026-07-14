# Second Compiler — Tier-1 Assisted Translation Playbook

The deterministic translator (**Tier 0**, [`calc-to-dax.md`](calc-to-dax.md)) owns only the
**provably-1:1 safe subset** of Tableau calcs. Everything it cannot translate faithfully stays an
**inert stub** with the original formula preserved — it is *never* force-fit into fragile DAX. The
hard, varied tail (table-calc addressing, INCLUDE/EXCLUDE LODs, parameters, regex, …) is **handed
off** to a second compiler.

> **The second compiler is the agent running this skill — not an embedded LLM API and not value
> materialization.** Tier 0 emits a structured, *categorized* translation request; the agent reads
> it, supplies the missing intent, authors a candidate DAX, and **validates** it (syntactic always,
> reconciliation oracle when data is landed). It is an **explicit, user-gated opt-in stage**: after the
> deterministic pass leaves a stub, the agent **presents the stubbed calcs and runs this pass only on an
> explicit `GO`** — if the user declines, the deterministic result ships as-is with every stub's
> `TableauFormula` preserved. **Once authorized, it automatically lands every candidate that passes
> validation** (no per-calc human-approval prompt). The deterministic guarantees never change, and
> **nothing goes live *unvalidated*** (an unverifiable candidate stays an inert stub).

This doc is the agent's operating contract. The deterministic *router* that categorizes each
fallback lives in [`scripts/translation_router.py`](../scripts/translation_router.py); the handoff
manifest is built by `translation_handoff_artifact` in
[`scripts/assemble_model.py`](../scripts/assemble_model.py). For *why* a given construct is Tier 0 vs
Tier 1 (the Axis-A/Axis-B boundary behind every category), see the
[Tier-1 charter](tier1-charter.md).

---

## Where the two compilers meet

```text
                         ┌──────────────────────────── Tier 0 (deterministic) ───────────────────────────┐
  Tableau calc  ─────▶   translate_tableau_calc_to_dax / _to_column_dax / table-calc seam
                         │                                                                                │
                 faithful?├── yes ─▶ LIVE DAX measure/column  (TranslatedBy = deterministic)              │
                         │                                                                                │
                          └── no ──▶ inert stub (formula preserved)  +  honest fallback_reason            │
                         └────────────────────────────────────┬───────────────────────────────────────┘
                                                               │
                              translation_router.classify_fallback(reason, role, fields)
                                                               │  category + guidance
                                                               ▼
                         report["translation_handoff"] = { summary, needs_review, requests[] }
                                                               │
                         ┌──────────────────────── Tier 1 (agent-as-second-compiler) ───────────────────┐
                         │  1. read request (category, guidance, formula, fields, target grain)          │
                         │  2. supply the missing INTENT for the category                                │
                         │  3. author the LEANEST faithful candidate DAX                                  │
                         │  4. VALIDATE  (check_candidate_dax always; reconciliation oracle when landed) │
                         │  5. LAND every VALIDATED candidate automatically via approved_calc_dax        │
                         │  6. report what landed + provenance/confidence; unverifiable stays a stub     │
                         └──────────────────────────────────────────────────────────────────────────────┘
```

Tier 0 emits live DAX for the safe subset; the second compiler (Tier 1) runs **when the user authorizes
it** — offered the moment the deterministic pass leaves any calc stubbed, and run only on an explicit
`GO`. Once authorized it lands every candidate that passes validation (the syntactic gate always, plus the
reconciliation oracle whenever data is landed) through the same `approved_calc_dax` path, with **no
separate per-calc human-approval step**; what cannot be validated stays an inert stub. If the user declines
the pass, the deterministic result ships as-is. Nothing lands *unvalidated* — the validation gate, not a
human prompt, is the faithfulness guarantee. This generalizes the existing assisted-translation landing
path (see SKILL.md § *Assisted translation*) from a fixed idiom registry to the full categorized handoff.

---

## When to start — offer it whenever a stub remains; run it on the user's `GO`

The second compiler is an **explicit, user-gated stage** — you **offer** it the moment the deterministic
(Tier 0) pass leaves any calc stubbed, and you **run** it only when the user authorizes it. You do not
proceed on your own; you also never silently skip it: whenever a calc is stubbed you **must present the
option** (a migration that quietly ships stubs without telling the user an assisted pass exists is a
process failure, even though the stub itself is a valid outcome).

Inspect the report the instant the deterministic pass returns:
`report["summary"]["needs_review_total"]` (estate path) or each datasource's
`report["translation_handoff"]["summary"]["needs_review"]` (direct path). When it is `> 0`, **STOP and
offer the pass** — present the stub summary and ask:

> `N of M calculations translated deterministically; K need review: <Calc A>, <Calc B>, …`
> `— run the LLM-assisted second compiler to attempt these? Reply GO to run it, or skip`
> `to ship the deterministic result as-is.`

Only **after the user replies `GO`** work the loop below for every needs-review calc. If the user declines,
ship the deterministic model as-is — every stub keeps its preserved `TableauFormula`, which is a complete,
honest outcome. The estate orchestrator writes the same list into `summary.md` under a **Next step —
second compiler (optional — offer to run)** heading, so the option is surfaced even when the run was
unattended. Never hand back a model with `= 0` stubs without having **offered** this pass.

The **faithful-or-stub** invariant binds at the *landing* step: **once the user authorizes the pass**, a
calc with no faithful DAX form (an unverifiable `dax_language_gap` approximation, an unrecoverable
addressing intent) stays an inert stub with its `TableauFormula` preserved. Authorized or not, you never
land a guess — the validation gate below is what enforces that, in place of a human approval prompt.

---

## The handoff request — what Tier 0 hands you

`report["translation_handoff"]` is purely additive and always present:

```jsonc
{
  "summary": {
    "total": 12, "live": 7, "needs_review": 5,
    "translated": 7, "assisted_approved": 0, "assisted_suggested": 1, "stub": 4,
    "coverage_pct": 58.3,
    "categories": {                       // counts across the needs-review calcs
      "missing_addressing_intent": 2,
      "model_object_parameter": 1,
      "missing_outer_aggregation": 1,
      "dax_language_gap": 1
    }
  },
  "needs_review": [                        // concise list for the check-in prompt
    { "name": "Running Sales", "role": "measure",
      "fallback_reason": "unsupported function RUNNING_SUM",
      "category": "missing_addressing_intent", "has_suggestion": false }
  ],
  "requests": [                            // one structured record per needs-review calc
    {
      "name": "Running Sales",
      "role": "measure",                   // measure | dimension
      "target_table": "_Measures",         // where a translated object would live
      "formula": "RUNNING_SUM(SUM([Sales]))",
      "fields": [                          // every resolved reference, typed
        { "caption": "Sales", "kind": "field", "table": "Orders", "column": "Sales", "type": "double" }
      ],
      "fallback_reason": "unsupported function RUNNING_SUM",
      "category": "missing_addressing_intent",
      "category_guidance": "This is a table calculation whose partition/order/scope …",
      "has_suggestion": false              // + "suggestion": {pattern, dax, …} when the idiom registry matched
    }
  ]
}
```

`fields[].kind` is one of `field` (resolved to `table`/`column`/`type`), `calc` (a reference to
another calculated field, with its `references_formula`), `parameter` (`[Parameters].[X]`), or
`unresolved`. That resolution is everything you need to translate at the right grain — you do not
have to re-parse the formula to discover its inputs.

> **Write every column reference in your DAX as `'<fields[].table>'[<fields[].column>]` — the
> resolved *model* identifiers, never the Tableau `caption`.** The engine lands your approved DAX
> **verbatim** against the generated model, whose column names are **sanitized** (a Tableau field
> such as `State/Province` becomes the model column `State_Province`; spaces, `/`, `,`, parentheses,
> etc. all collapse to `_`). Authoring against the caption — `'Orders'[State/Province]` — yields a
> model that *deserializes* (Gate 0 green) but **errors at query/refresh time** with
> *"Column 'State/Province' in table 'Orders' cannot be found."* Authoring against the resolved
> `column` — `'Orders'[State_Province]` — binds correctly. `fields[].caption` is for *reading* the
> original formula; `fields[].table` / `fields[].column` are what you *emit*.

---

## The category taxonomy — your routing map

Each category is a **distinct playbook**. Read `category`, then do the matching work below. The full
guidance string ships in the request as `category_guidance`.

| Category | What it means | Intent you must supply | Target DAX shape |
|---|---|---|---|
| `model_object_parameter` | The calc is driven by a Tableau **parameter** — a Power BI *model object*, not an expression. | Which **swap type**: measure swap, dimension swap, or what-if. | **Reuse the deterministic emitters in `parameters.py`** — don't hand-author. `detect_field_swap` classifies a swap; `emit_field_parameters` builds a field-parameter table (measure *and* dimension swaps); `emit_value_parameters` builds the what-if table + `[<Param> Value]` measure and returns a `param_resolver`. A calc group is the richer measure-swap alternative. Rebind the calc to the selected value. |
| `missing_addressing_intent` | A **table calc** whose partition/order/scope (Tableau "Compute Using") is not in the `.tds`. | The **addressing** — partition + order — ideally recovered from worksheet context (`.twb`). | cumulative → running total / time-intelligence; prior/offset → `OFFSET`; rank → `RANKX` over the partition; size/row-number → `COUNTROWS`/`RANKX` over `ALLSELECTED`. |
| `missing_outer_aggregation` | An **LOD** whose result depends on the visual's dimensionality (INCLUDE/EXCLUDE, bare LOD, non-superset nested LOD). | The intended **grain** and outer aggregation. | INCLUDE → `CALCULATE` over an added group; EXCLUDE → `CALCULATE(…, REMOVEFILTERS(dims))`; bare LOD → an explicit outer aggregate. |
| `dax_language_gap` | **No faithful native DAX form exists** (regex, arbitrary `DATEPARSE`, general `SPLIT`, `FINDNTH`, case-sensitive ordered text, exotic date part). | Whether the *real* usage is narrow enough to approximate safely. | An **approximation** only (e.g. `PATH`/`SUBSTITUTE` for a fixed delimiter, a known date format) — **flagged approximate** and oracle-verified, else keep the stub. |
| `type_or_shape_mismatch` | A typing/parse/shape refusal (inconsistent IF/CASE branches, incomparable operands, 4-arg `IIF`, an aggregate inside a row-level column calc). | An explicit cast, aligned branch types, or a measure-vs-column re-route. | The repaired expression, then **re-run Tier 0** — often it then translates deterministically. |
| `unresolved_reference` | A field/dimension/calc could not be bound (unresolved/ambiguous name, cross-table terms, unsupported type). | The correct table binding / relationship, or the referenced calc translated first. | Usually **no new DAX** — fix the binding and re-run Tier 0. |
| `unsupported_other` | Unmatched. A faithful form may still exist (e.g. `CORR`/`COVAR`/`COVARP` via a `VAR`/`RETURN` closed form). | Author and validate a candidate at the right grain. | The leanest faithful form; validate before proposing. |

> **`unresolved_reference` and `type_or_shape_mismatch` are the cheapest wins** — they frequently
> need *no* second-compiler DAX at all: fix the reference or add a cast and the deterministic tier
> translates the calc on the next pass. Always try Tier 0 again before authoring bespoke DAX.

---

## The output contract — what you produce per request

For every candidate you propose, supply:

1. **`dax`** — the candidate expression, the **leanest faithful shape** a competent Power BI modeler
   would actually build (see the leanness ladder below).
2. **`provenance`** — how you derived it (e.g. "workbook Compute-Using: partition {Category}, order
   Order Date"; "INCLUDE grain = {Customer}"). The original formula stays preserved as
   `TableauFormula`; landed candidates are stamped `TranslatedBy = assisted translation
   (human-approved)`.
3. **`confidence`** — high / medium / low, honest about residual ambiguity.
4. **`caveats` / cost line** — the model cost and the contexts where it could differ (e.g. "1 calc
   column; per-row partition scan"; "assumes natural sort on the addressing dimension"; "approximate
   — fixed `-` delimiter only"). This is what lets the user judge *"is this reasonable?"* before
   approving.

---

## The validation gate — never skip it

A candidate is **not** acceptable just because it parses.

- **Always** run the deterministic syntactic gate `translation_router.check_candidate_dax(dax, request=req)`
  first. It returns `{"ok", "issues", "warnings"}`: it balances parens **and** brackets and quotes
  (stricter than `calc_to_dax.validate_dax`, which checks parens/quotes only), rejects a candidate
  that is merely the inert stub (`0` / `BLANK()`), and rejects leftover un-translated Tableau idioms
  (`{FIXED …}` / `{INCLUDE …}` / `{EXCLUDE …}` braces, `[Parameters].[…]` references). If `ok` is
  `False`, **fix the candidate before going further** — never propose a candidate the gate rejects.
  (`calc_to_dax.validate_dax(dax)` remains available as the lower-level parens/quotes check.)
- **When data is landed**, run the **reconciliation oracle** ([`validation-reconciliation.md`](validation-reconciliation.md)):
  evaluate the candidate against the live Power BI model and compare to the Tableau value (VizQL Data
  Service) **at a fixed grain** within tolerance. Accept only on match; otherwise keep the honest
  stub and mark it review-needed. This is the non-circular proof of faithfulness. The deterministic
  core of this compare is [`scripts/translation_reconcile.py`](../scripts/translation_reconcile.py)
  (`reconcile` / `reconcile_request` / `reconcile_all`): it gates the candidate, builds the
  `EVALUATE ROW(…)` probe, and applies the tolerance policy, taking the Fabric and Tableau backends
  as **injected** `fabric_oracle` / `tableau_value` hooks (nothing runs silently). It returns a
  `verified` / `mismatch` / `not-evaluated` record per candidate.
- **When you author more than one candidate**, rank them by the oracle rather than by eye:
  `translation_reconcile.rank_candidates(name, [dax1, dax2, …], fabric_oracle=…, tableau_value=…)`
  reconciles each (gate → numeric oracle) and returns them **best-first**, each with a `confidence`
  (`high` = verified against the Tableau value · `medium` = passed the gate but not yet reconciled ·
  `low` = proven wrong or malformed) and a one-line `reason`, plus `best` (the top non-`low`
  candidate, or `None` when every candidate is low — author a better one). Each candidate may be a
  raw DAX **string** or a suggestion dict carrying it under `dax` (the `suggest_assisted_dax` shape),
  so you can rank the idiom-registry suggestions directly; `best` is always the resolved DAX string,
  ready to hand to `approved_calc_dax`. Each ranked entry also carries an auditable `signals`
  breakdown (`{gate, oracle, category}`) behind its grade, and a `requires_oracle` flag: for a
  `dax_language_gap` approximation the oracle match is **mandatory**, so such a candidate is **never**
  returned as `best` until it is VERIFIED (it stays listed at its medium grade for you to reconcile or
  revise) — the same faithful-or-stub rule the gate enforces, applied to selection. This is the
  optional acceleration tier's **selection** step: it ranks by **semantic equivalence, not string
  similarity**, and — like everything in Tier 1 — lands nothing itself; the chosen candidate still flows
  through `approved_calc_dax` after clearing the validation gate. Its `confidence` is the **semantic**
  signal that feeds the §output-contract `confidence` field above.
- For a `dax_language_gap` approximation, the oracle match is **mandatory** before proposing — an
  unverifiable approximation stays a stub. (The syntactic gate emits a warning reminder for this
  category when you thread the request through as `request=`.)

---

## Landing a validated candidate

Landing reuses the existing two-pass path — no new mechanism:

```python
from assemble_model import migrate_tds_to_semantic_model

# Pass 1 — see the categorized handoff (nothing is live yet):
out = migrate_tds_to_semantic_model(tds_text, model_name="Superstore", calcs=calcs)
ho = out["report"]["translation_handoff"]
# group ho["requests"] by ["category"]; author + validate candidates for the ones you can.

# Pass 2 — flip every VALIDATED candidate into a live object (automatic; no human-approval step):
approved = {"Running Sales": "<validated candidate DAX>"}          # {calc_name: dax}, case-insensitive
final = migrate_tds_to_semantic_model(tds_text, model_name="Superstore",
                                      calcs=calcs, approved_calc_dax=approved)
```

- Landing is **batch, not per-calc** — validate all the candidates you can (syntactic gate always,
  oracle when data is landed), then land the whole validated set in one pass. There is no per-calc
  human-approval prompt; the validation gate is what authorizes a candidate to land.
- Every second-compiler field is named with a leading **`!`** (configurable) so it stands out for
  review and clusters at the top of the field list; the `!` means *"not from the trusted
  deterministic path."* Verified vs. unverified state lives in the `TranslatedBy` metadata — names
  never change on verification (renaming would re-break references). Landing is **idempotent** (never
  `!!`).

---

## Hard safety invariants (all tiers)

1. **Faithful-or-stub.** Anything correct only in a narrow context is a stub or a validation-gated
   candidate — never silent, unvalidated live DAX.
2. **Tier 0 is untouched.** The second compiler only adds validation-gated candidates; it never
   changes the deterministic output or its guarantees.
3. **Landing stays explicit; the run is the user's opt-in.** A single migrate call with no
   `approved_calc_dax` adds ZERO live assisted objects — landing always happens on the explicit
   `approved_calc_dax` pass, so the deterministic default is never mutated implicitly. The agent
   performs that landing pass **only after the user authorizes the second-compiler stage** (a `GO`);
   once authorized, landing every validated candidate is automatic and needs no per-calc approval.
4. **Leanness ladder — stop at the FIRST faithful rung:** (1) inline expression in a single
   measure/column → (2) one extra calculated column → (3) a small bounded set of cooperating objects
   → (4) a real dimension table + relationship *when a PBI modeler genuinely would* → (5) honest stub
   + recommendation. Never escalate a rung just to avoid a handoff; never fan out dozens of objects
   for one idiom.
5. **Provenance is the source of truth.** Always preserve `TableauFormula`; stamp `TranslatedBy`; the
   `!` prefix is a derived display signal, not the only record.

---

## Worked example — a parameter (what-if)

```text
request.category = "model_object_parameter"
formula          = "[Sales] * (1 + [Parameters].[Growth Rate])"
fields           = [ {caption:"Sales", kind:"field", …},
                     {caption:"[Parameters].[Growth Rate]", kind:"parameter"} ]
```

1. **Intent:** the parameter is a single numeric value the user sweeps → **what-if**.
2. **Model object (deterministic — don't hand-author):** parse the parameter from the `.twb`/`.tds`
   with `parse_parameters`, then call `emit_value_parameters(params, calcs=[…])`. It emits the
   disconnected `Growth Rate Parameter` table (`GENERATESERIES(min, max, step)` from the parameter's
   own range) + a `Growth Rate Value` = `SELECTEDVALUE(...)` measure, and returns a `param_resolver`
   that inlines `[Parameters].[Growth Rate]` as `[Growth Rate Value]`.
3. **Candidate:** feed that `param_resolver` to the calc translator (`translate_tableau_calc_to_dax(
   formula, resolve, param_resolver=…)`) — Tier 0 then translates the host calc deterministically to
   `SUMX('Orders', 'Orders'[Sales]) * (1 + [Growth Rate Value])`. You author bespoke DAX only if the
   usage falls outside the emitter's grammar.
4. **Validate:** `check_candidate_dax` ✓ (balanced, not a stub, no leftover Tableau idioms); oracle
   at a fixed Growth Rate value vs Tableau with the same parameter ✓.
5. **Land:** note the cost ("adds 1 disconnected parameter table + 1 measure; value follows the
   slicer") in the report, then land automatically via `approved_calc_dax` — no approval prompt.
