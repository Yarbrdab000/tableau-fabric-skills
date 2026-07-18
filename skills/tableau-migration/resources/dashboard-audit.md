# Dashboard Audit — Tier-3 Assisted Visual Playbook

The deterministic viz rebuild (**Tier 0 for dashboards**, [`viz-rebuild.md`](viz-rebuild.md)) rebuilds
each Tableau worksheet as a model-bound PBIR visual — chart type, exact field bindings, roles, layout,
slicers — for the subset it can rebuild **faithfully**. Every visual it is *unsure* about it does not
guess: it emits the visual it can defend and **flags** it (`viz_fidelity` `status: "warned"`; a
`needs_attention` entry in the per-workbook remediation worklist). The flag is the handoff — the exact
counterpart of a calc **stub**.

> **The dashboard auditor is the agent running this skill — the same adjudication tier as the calc
> second compiler, pointed at visuals instead of DAX.** Tier 0 rebuilds the safe subset and flags the
> rest; **you** read each flagged visual, decide whether a faithful improvement exists, and let the
> **monotonic fidelity gate** prove it is `>=` the deterministic rebuild before it is kept. It is an
> **explicit, user-gated opt-in stage**: after the deterministic rebuild flags visuals, the agent
> **presents the warned visuals and runs this pass only on an explicit `GO`** — if the user declines,
> the deterministic rebuilt report ships as-is. **Once authorized, every proposal is gate-validated and
> only non-regressing improvements are kept** (no per-visual approval prompt). The deterministic
> guarantees never change, and **nothing that regresses the rebuild is ever kept** — a proposal that is
> not a strict improvement reverts to the exact deterministic visual.

This doc is the agent's operating contract for Tier 3. It is deliberately the **same shape** as the
calc [second-compiler.md](second-compiler.md): a deterministic tier proves-or-flags, the agent
adjudicates the hard tail, and a **validation gate** — here the monotonic fidelity gate, there
`check_candidate_dax` + the reconciliation oracle — is the sole authority on what lands. Read that doc
first if you have not; this one assumes its vocabulary.

The machinery you stand on is all in this repo: the per-visual **remediation worklist**
([`scripts/remediation_worklist.py`](../scripts/remediation_worklist.py)), the read-only chart-type
**advisor** ([`scripts/viz_advisor.py`](../scripts/viz_advisor.py)), the audit **producer/selector**
([`scripts/audit_tier.py`](../scripts/audit_tier.py)), and the **monotonic fidelity gate**
([`scripts/monotonic_gate.py`](../scripts/monotonic_gate.py)).

---

## Read this first — you ARE the dashboard auditor

**The dashboard auditor is you, the agent. It is the act of adjudicating each warned visual, not a
script you run.** Tier 0 has already rebuilt the dashboard and flagged the visuals it could not defend.
**Nothing else is going to fix them.** You read each flagged visual, decide whether a faithful
improvement exists (a better chart type from its *listed* alternatives, a colour/label/legend
refinement that its worklist items call for), and hand the proposal to the gate — which keeps it only
if it regresses nothing.

**There is no script that redesigns a visual for you. Do not go looking for one.** The scripts whose
names *sound* like "the auditor" are narrow helpers — none of them decides anything:

| Script / flag | What it actually does | What it is **not** |
|---|---|---|
| `remediation_worklist.build_worklist` · `twb_to_pbir --worklist` | The viz **residual**: one entry per rebuilt visual, `ok` / `needs_attention`, with a severity + remediation hint per gap. | Not a fixer. It describes gaps; it repairs none. |
| `viz_advisor.build_report_advice` · `migrate_estate --viz-advice` | The **advisor**: ranks *candidate* chart-type alternatives per visual (Tier-2, deterministic). | Not a decision. It proposes options; **you** pick from them. |
| `audit_tier.build_dashboard_audit` · `audit_tier <in> --prompt` · `twb_to_pbir --audit` | The **producer**: folds worklist + advisor into ONE priority-ordered, full-dashboard audit bundle (every visual, its items, its allowed alternatives, the hard rules). | Not authoring. It hands you the whole surface to adjudicate; it changes no PBIR. |
| `audit_tier.land_dashboard_audit` · `monotonic_gate` | The **gate**: scores each proposed visual against its deterministic baseline and keeps it **only if it regresses nothing**. | Not a designer. It selects between two already-built visuals; it invents none. |

So the loop is: **you** adjudicate → the monotonic gate **validates** → only strict improvements are
kept. If you ever find yourself hunting for a command that "runs the dashboard auditor," stop — that
command is you deciding, visual by visual, which flagged rebuild to improve.

---

## Where the two tiers meet

```text
                         ┌───────────────── Tier 0 (deterministic viz rebuild) ─────────────────┐
  Tableau worksheet ──▶  twb_to_pbir: rebuild PBIR visual (type + field bindings + roles + layout)
                         │                                                                        │
                 faithful?├── yes ─▶ LIVE PBIR visual (viz_fidelity status = rebuilt)             │
                         │                                                                        │
                          └── no ──▶ best-defensible visual + honest flag (status = warned)       │
                         └───────────────────────────────┬────────────────────────────────────────┘
                                                          │
                    remediation_worklist.build_worklist(warnings, candidate_records)
                                     + viz_advisor ranked alternatives
                                                          │
                         ┌──────────────────────── Tier 3 (you, gated) ──────────────────────────┐
                         │  0. OFFER the warned visuals; run only on GO                            │
                         │  1. BUILD the audit bundle  (build_dashboard_audit / --audit)           │
                         │  2. ADJUDICATE each visual  (type only from listed alternatives)        │
                         │  3. GATE every proposal     (land_dashboard_audit / monotonic_gate)     │
                         │  4. KEEP only strict improvements (match-or-beat, per visual)           │
                         └────────────────────────────────────────────────────────────────────────┘
```

The parallel with the calc tier is exact:

| | Calc (Tier 1) | Dashboard (Tier 3) |
|---|---|---|
| Residual / handoff | `translation_handoff` stubs | worklist `needs_attention` + `viz_fidelity` `warned` |
| Loop doc | [second-compiler.md](second-compiler.md) | **this doc** + `audit_prompt()` runbook |
| You produce | faithful DAX candidate | a faithful visual improvement (chart type from listed alternatives; colour/label/legend refinement) |
| Validation gate | `check_candidate_dax` + reconciliation oracle | **monotonic fidelity gate** (`land_dashboard_audit`) |
| Guarantee | faithful-or-stub | match-or-beat, per visual (regressing proposals revert) |

---

## The gated invocation contract (identical to the calc tier)

1. **The deterministic rebuild runs first and ships on its own.** A run with no audit is byte-identical
   to a run with one; opting in adds only gate-validated improvements.
2. **The residual is surfaced honestly.** Offer Tier 3 exactly when the rebuild flagged something —
   `viz_fidelity` `status == "warned"`, or worklist `summary.visuals_flagged > 0` (equivalently the
   audit bundle's `summary.needs_attention > 0`).
3. **Offer is mandatory; never silent, never auto-run.** If visuals are warned you must present them.
4. **The user gates it with `GO`.** Decline → the deterministic rebuilt report ships as-is.
5. **Once authorized, you ARE the auditor** — you adjudicate every visual the bundle lists.
6. **The monotonic gate is the authority.** Every proposal goes through `land_dashboard_audit` /
   `monotonic_gate`; there is no per-visual human-approval prompt — the gate is what authorizes a
   change to be kept.
7. **Nothing regresses.** A proposal is kept **only** if it regresses no scored component vs the
   deterministic visual; a mixed or worse change reverts to the exact deterministic object.
8. **The deterministic default is untouched.** The rebuild path stays byte-identical whether or not an
   audit runs.

---

## Step 0 — offer, then run only on GO

Guard: only when a workbook has warned visuals. Present them plainly and wait for `GO`:

> "P of Q visuals rebuilt faithfully; R warned: `<Visual A>` on `<Sheet>`, `<Visual B>` on `<Sheet>`,
> … — run the LLM-assisted dashboard audit to attempt these? Reply `GO` to run it, or skip to ship the
> deterministic rebuild as-is."

No `GO` → stop; the deterministic rebuilt `pbip/<Workbook>/` is the honest, complete result. A warned
visual is not a failed rebuild — it is a defensible rebuild the engine declined to over-claim.

---

## Step 1 — build the audit bundle

The bundle enumerates **every** rebuilt visual (warned *and* clean), highest-priority first, each with
its worklist items and its allowed chart-type `alternatives`, plus the hard `rules`. Build it from
whatever you have:

```text
# From a .twb (or a saved migration-result JSON):
py -3.11 "$SKILL\scripts\audit_tier.py" <input.twb|result.json> -o audit.json
py -3.11 "$SKILL\scripts\audit_tier.py" <input.twb|result.json> --prompt   # print the runbook prompt

# Or alongside a report rebuild:
py -3.11 "$SKILL\scripts\twb_to_pbir.py" <input.twb> -o <out> --audit audit.json
```

In-process (single workbook already migrated):

```python
from audit_tier import build_dashboard_audit
bundle = build_dashboard_audit(result["worklist"], result.get("candidate_records", []), intent="...")
```

Each `bundle["visuals"][n]` carries: `worksheet`, `visual`, `page`, `current_type`, `status`,
`priority`, `items` (severity / category / reason / remediation), and — when the advisor found any —
`alternatives` (the **only** chart types you may switch this visual to) and `top_alternative`. Items
that belong to no single visual (dashboard-scope, parameter controls) ride in `unattached_items`, so
nothing is dropped.

---

## Step 2 — adjudicate each visual (the hard rules)

Work highest-priority first (`blocking` → `high` → `medium` → `low`). For each visual, decide: **keep
the deterministic rebuild**, or **propose a faithful improvement**. These rules are enforced by charter
(`audit_tier.AUDIT_RULES`) — follow them exactly:

1. **Audit every visual listed**, not only the flagged ones — the whole dashboard is in scope.
2. **Highest-priority first** (blocking, then high, then medium, then low).
3. **Never change the source-field truth** — never drop, add, or re-role a field the deterministic
   rebuild bound. This is the viz twin of the calc tier's faithful-or-stub: data is not yours to move.
4. **Chart type only from a visual's listed `alternatives`** (or keep its `current_type`). Do not
   invent a type that is not offered — the advisor offers only types whose field grammar the bound
   fields already satisfy.
5. **Never strip** a legend, colour scale, or data label the deterministic rebuild already produced.
6. **Every proposal is monotonic-gated** — aim for a *genuine* improvement, because a regressing change
   is reverted for you (§step 3). A pure improvement (a warned type corrected to the right alternative,
   a missing colour ramp restored) lands; a lateral or mixed change does not.
7. **A visual with no `alternatives`** (e.g. a detail table) is still addressed via faithful
   formatting/colour/label refinements from its items — **never invent data**.

The `intent` you pass to the bundle biases the advisor's ranking (e.g. "this is an executive summary,
prefer KPI cards"), but it never widens the allowed set — you still pick only from `alternatives`.

---

## Step 3 — the validation gate (never skip it)

A proposal is **not** kept because it looks better. It is kept only because the **monotonic fidelity
gate** proves it regresses nothing. Run every `(deterministic_visual, assisted_visual)` pair through it:

```python
from audit_tier import land_dashboard_audit
pairs = [(twb_ws, det_visual, assisted_visual, zone), ...]   # zone optional
result = land_dashboard_audit(pairs, audit=bundle)
# result["visuals"]  -> the chosen visual per pair (assisted iff it strictly did not regress)
# result["decisions"][n]["kept_assisted"] -> True only when the gate kept your proposal
# result["summary"]  -> assisted_kept / reverted, and (with the bundle) flagged_visuals / flagged_improved
```

What the gate scores (`monotonic_gate`): **structural** axes reused from the fidelity oracle — visual
type, field set, field roles, canvas position (the "same chart of the same data" guarantee) — **and**
**feature** axes it computes off the emitted PBIR object — continuous colour-fill richness, per-point
colour, data labels, legend, title. A proposal is kept **only if every scored component is `>=`
baseline** (an `epsilon` absorbs float jitter). A *mixed* change (improves one axis, regresses another)
is **reverted**, not kept — re-propose it as a pure, non-regressing improvement if it is worth landing.
Because a revert returns the exact deterministic object, opting into Tier 3 can only ever **match or
beat** the deterministic report, per visual. That is the hard guarantee, by construction — the viz twin
of "nothing goes live unvalidated."

---

## Step 4 — outcome, and where landing stands today

`land_dashboard_audit` returns the **gate-validated result**: the chosen visual per pair (your
improvement only where it strictly did not regress), the per-visual `decisions`, and a summary of how
much of the *flagged* work the assisted pass actually improved (`flagged_improved` / `flagged_total`).
This is the audit's verdict: exactly which warned visuals have a proven-safe improvement, and which
keep their deterministic rebuild.

> **On-disk landing seam — honest status.** The first-class `--approved-viz` re-emit seam (the viz twin
> of the calc tier's `--approved-dax`: author a JSON of per-visual choices, re-run, and have the PBIR
> re-emitted with the gate-kept improvements) is a **separate, later increment**. In this version the
> Tier-3 loop produces the **gate-validated verdict in-process**, and the **deterministic PBIR is the
> shipped baseline**. Do **not** hand-edit a rebuilt `visual.json` to a new chart type to "land" a
> change — swapping `visualType` without rebuilding the visual's projection wells can emit an invalid
> visual, which is exactly the un-gated regression this tier exists to prevent. Report the verdict
> (which visuals have a proven improvement, at what priority) and ship the deterministic rebuild until
> the seam lands.

Formatting-only refinements that do **not** change the visual type or its bindings (a colour, a font, a
legend toggle) remain safe to apply to the emitted PBIR by hand, as the Post-Migration preview step
already notes — those cannot invalidate the visual.

---

## Hard safety invariants (all tiers)

1. **Match-or-beat, per visual.** A proposal is kept only if it regresses nothing; otherwise the exact
   deterministic visual stands. Never keep a change on a hunch.
2. **Tier 0 is untouched.** The audit tier is a producer + selector; it never changes the deterministic
   rebuild or its guarantees, and the default path stays byte-identical.
3. **The offer is the user's opt-in; keeping is the gate's call.** You always offer when visuals are
   warned; you run only on `GO`; once authorized, the *gate* — not a per-visual prompt — decides what is
   kept.
4. **Source-field truth is inviolate.** Never drop, add, or re-role a bound field, and never strip a
   legend / colour / label the rebuild produced. Chart type changes only within a visual's listed
   `alternatives`.
5. **Never invent data.** A visual with no faithful improvement keeps its deterministic rebuild — a
   warned visual honestly flagged is a complete outcome, not a failure.

---

## Worked example — a warned dual-axis view

```text
bundle.visuals[0] = {
  worksheet: "Sales by Region", visual: "map-1", current_type: "filledMap",
  status: "needs_attention", priority: "high",
  items: [ {severity:"high", category:"chart_type",
            reason:"dual-axis overlay flattened to a single filled map",
            remediation:"consider a layered map or a clustered bar if the overlay is categorical"} ],
  alternatives: [ {visual_type:"map"}, {visual_type:"clusteredColumnChart"} ],
  top_alternative: {visual_type:"map"}
}
```

1. **Adjudicate:** the overlay is geographic, not categorical → the faithful improvement is `map` (a
   layered point/bubble map), which is in `alternatives`. `clusteredColumnChart` is offered but would
   drop the geography — reject it. Keep every bound field and the colour scale.
2. **Build the proposal:** the assisted PBIR visual object = the deterministic one with `visualType`
   rebuilt as `map` (bindings unchanged) — built through the normal visual builder, **not** a raw
   `visualType` string edit.
3. **Gate:** `land_dashboard_audit([(ws, det_visual, map_visual, zone)], audit=bundle)`. The gate scores
   both; the `map` keeps every field/role/position and restores the intended overlay → no regression →
   `kept_assisted: True`.
4. **Outcome:** the verdict records `map-1` as a proven high-priority improvement; a `clusteredColumn`
   proposal would have regressed the structural (field-set) axis and reverted. Ship per §step 4.
