"""The shipped report and the described report are different artifacts, and now say so (#173).

The engine writes each report TWICE:

  reports/<WB>.Report            the first, model-UNBOUND pass -- what viz_fidelity, the
                                 remediation worklist and the visual-calc rollups are computed from
  pbip/<WB>/<WB>.Report          a model-BOUND re-run inside the openable project -- what a user
                                 actually opens

The second pass knows things the first cannot (a calc that became a real measure, a row count that
resolved), so a visual can legitimately be routed differently. That is BY DESIGN and this block does
not call it a defect.

What was missing is that nobody could SEE it. Measured at 2.352.0, ``report.json`` contained zero
occurrences of "pre-rebind", "unbound", "tree_divergence" or any equivalent, so a reader comparing a
fidelity tier against the report they opened was comparing two different objects with nothing
saying so. That is the population-mismatch failure this project treats as the expensive class.

Measured on the 34-workbook corpus at the release that added this:

    0075_customers_above_average   tableEx[Values]              -> clusteredBarChart[Category, Y]
    0079_active_or_open_items      tableEx[Values]              -> clusteredColumnChart[Category, Y]
    0088_salesforce_nonprofit      clusteredBarChart[Category,Y]-> barChart[Category, Series, Y]

Upstream #173 reached the same mechanism from the OPPOSITE direction: a scatter whose shipped copy
*lost* a grouping role while the described copy kept it -- ``rebuilt`` in a tree nobody opens. Ours
gain roles. A single "shipped is strictly worse" model fits neither, which is why the block reports
the two shapes rather than a verdict.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import migrate_estate as M  # noqa: E402


def _write(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)


def _visual(vt, roles):
    return {"visual": {"visualType": vt,
                       "query": {"queryState": {r: {"projections": []} for r in roles}}}}


def _estate(tmp, described, shipped):
    """Build the two trees the way the engine lays them out, and return a ``detail`` skeleton."""
    rel = "definition/pages/p/visuals/v/visual.json"
    _write(os.path.join(tmp, "reports", "WB.Report", *rel.split("/")), described)
    _write(os.path.join(tmp, "pbip", "WB", "WB.Report", *rel.split("/")), shipped)
    return {"output_folder": "reports/WB.Report", "pbip_folder": "pbip/WB/WB.pbip"}


def test_a_divergent_visual_is_NAMED_with_both_shapes(tmp_path):
    tmp = str(tmp_path)
    detail = _estate(tmp,
                     _visual("tableEx", ["Values"]),
                     _visual("clusteredBarChart", ["Category", "Y"]))
    M._attach_shipped_tree_divergence(detail, write_root=tmp)
    b = detail["shipped_tree_divergence"]
    assert b["compared"] == 1
    assert len(b["differing"]) == 1
    d = b["differing"][0]
    # Both shapes, not a verdict: the divergence runs in both directions across estates, so a
    # reader needs what changed rather than "worse".
    assert d["described_visual_type"] == "tableEx"
    assert d["shipped_visual_type"] == "clusteredBarChart"
    assert d["described_roles"] == ["Values"]
    assert d["shipped_roles"] == ["Category", "Y"]


def test_a_ROLE_only_change_is_caught_even_when_the_type_matches(tmp_path):
    """The upstream shape: same visualType, a grouping role dropped. A type-only comparison would
    call this identical -- which is exactly how a scatter ships ungrouped while reading `rebuilt`."""
    tmp = str(tmp_path)
    detail = _estate(tmp,
                     _visual("scatterChart", ["Category", "X", "Y"]),
                     _visual("scatterChart", ["X", "Y"]))
    M._attach_shipped_tree_divergence(detail, write_root=tmp)
    d = detail["shipped_tree_divergence"]["differing"]
    assert len(d) == 1
    assert d[0]["described_roles"] == ["Category", "X", "Y"]
    assert d[0]["shipped_roles"] == ["X", "Y"]


def test_identical_trees_report_PRESENT_AND_EMPTY(tmp_path):
    """"No divergence" must be distinguishable from "not evaluated" -- same contract as
    reference_case_mismatches. An absent key would make a healthy build and an unreadable one look
    the same."""
    tmp = str(tmp_path)
    same = _visual("clusteredBarChart", ["Category", "Y"])
    detail = _estate(tmp, same, same)
    M._attach_shipped_tree_divergence(detail, write_root=tmp)
    b = detail["shipped_tree_divergence"]
    assert b["differing"] == []
    assert b["compared"] == 1
    assert "described_tree" in b and "shipped_tree" in b


def test_role_ORDER_is_not_a_divergence(tmp_path):
    """PBIR role ordering is not meaningful; reporting it would drown the real signal."""
    tmp = str(tmp_path)
    detail = _estate(tmp,
                     _visual("clusteredBarChart", ["Y", "Category"]),
                     _visual("clusteredBarChart", ["Category", "Y"]))
    M._attach_shipped_tree_divergence(detail, write_root=tmp)
    assert detail["shipped_tree_divergence"]["differing"] == []


def test_files_present_in_only_one_tree_are_COUNTED_not_silently_skipped(tmp_path):
    """A visual with no counterpart cannot disagree with anything, so a bare `differing` count would
    read as agreement. The two only-in counts are what make a zero mean something."""
    tmp = str(tmp_path)
    detail = _estate(tmp,
                     _visual("card", ["Values"]),
                     _visual("card", ["Values"]))
    extra = "definition/pages/p/visuals/EXTRA/visual.json"
    _write(os.path.join(tmp, "pbip", "WB", "WB.Report", *extra.split("/")),
           _visual("slicer", ["Values"]))
    M._attach_shipped_tree_divergence(detail, write_root=tmp)
    b = detail["shipped_tree_divergence"]
    assert b["shipped_only"] == 1
    assert b["described_only"] == 0
    assert b["compared"] == 1


def test_it_never_fails_a_build_when_a_tree_is_missing(tmp_path):
    """Best-effort and additive: a workbook with no pbip project must be byte-identical to before."""
    detail = {"output_folder": "reports/WB.Report"}
    M._attach_shipped_tree_divergence(detail, write_root=str(tmp_path))
    assert "shipped_tree_divergence" not in detail
    detail2 = {"output_folder": "reports/WB.Report", "pbip_folder": "pbip/WB/WB.pbip"}
    M._attach_shipped_tree_divergence(detail2, write_root=str(tmp_path))
    assert "shipped_tree_divergence" not in detail2


def test_the_note_says_which_tree_the_fidelity_describes(tmp_path):
    """The disclosure is the whole point -- a reader must be able to learn, from report.json alone,
    that viz_fidelity is not about the report they opened."""
    tmp = str(tmp_path)
    same = _visual("card", ["Values"])
    detail = _estate(tmp, same, same)
    M._attach_shipped_tree_divergence(detail, write_root=tmp)
    note = detail["shipped_tree_divergence"]["note"]
    assert "viz_fidelity" in note
    assert "UNBOUND" in note
    assert "pbip/" in note and "opens" in note
