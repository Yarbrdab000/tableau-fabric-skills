"""A Tableau SET named as a filter column must not vanish silently (#185).

A set is serialised as a ``<group name='[Set 1]'>`` holding a nested ``<groupfilter>`` tree -- it is
NOT a ``<column>``, so it never enters ``base_cols``. Before this change the filter loop called
``_resolve_field``, got ``None``, emitted the generic *"could not resolve field 'Set 1' (skipped)"*
and did ``continue`` -- the visual then rendered over an UNFILTERED SUPERSET.

The generic message is the problem, not merely its absence. It is emitted from ONE site that never
consults ``for_filter``, so these two produce identical text and have OPPOSITE consequences:

    dropped SHELF pill  -> the visual is VISIBLY missing a column
    dropped FILTER      -> the visual looks COMPLETE and shows more rows than Tableau

Measured on the 34-workbook corpus: 65 ``<group>`` elements, 56 uniquely named. 26 are Tableau's own
``Action (...)`` / ``Tooltip (...)`` dashboard machinery which never reaches the resolver (0
occurrences in ``report.json``), 28 are defined and never referenced, and exactly TWO are used --
``0063 Remove Nulls`` (range) as a FILTER, and ``0078 Set 1`` (top-n) as a shelf pill.

SCOPE, stated because the corpus cannot prove the impact: 0063's worksheet ALSO carries a
categorical ``Customer Segment IN ("Consumer")`` which is strictly stricter than the range set, so
the emitted numbers already match Tableau there. This release makes the drop LOUD and SPECIFIC; it
does not yet translate sets. The translation targets are researched and recorded in the CHANGELOG.
"""
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import twb_to_pbir as T  # noqa: E402
import remediation_worklist as RW  # noqa: E402


def _wb(groups_xml):
    return ET.fromstring(
        "<workbook><datasources><datasource name='ds1'>"
        + groups_xml +
        "</datasource></datasources></workbook>")


RANGE_SET = ("<group name='[Remove Nulls]'>"
             "<groupfilter from='&quot;Consumer&quot;' function='range' "
             "level='[Customer Segment]' to='&quot;Small Business&quot;'/></group>")

TOPN_SET = ("<group caption='Top Customers by Profit' name='[Highest profit customers]'>"
            "<groupfilter count='[Parameters].[Parameter 2]' end='top' function='end' "
            "units='records'>"
            "<groupfilter direction='DESC' expression='SUM([Profit])' function='order'>"
            "<groupfilter function='level-members' level='[Customer Name]'/>"
            "</groupfilter></groupfilter></group>")

ACTION_SET = ("<group name='[Action (Region)]'>"
              "<groupfilter function='crossjoin'>"
              "<groupfilter function='level-members' level='[Region]'/>"
              "</groupfilter></group>")

# A crossjoin that WRAPS an end/order -- a multi-dimension DYNAMIC set. This is the only shape in
# which two kind-needles match one group, so it is the only fixture that can exercise precedence at
# all. Added because a control that reordered the precedence table was NOT caught: every other
# fixture matches exactly one needle, so the ordering was unexercised and could have been any
# permutation. (Tableau documents dynamic sets as single-dimension only, so this is the illegal-ish
# corner -- which is precisely why it must classify as the more specific `top-n` and not be
# flattened to `crossjoin`, whose translation is a plain member list.)
CROSSJOIN_WRAPPING_END = (
    "<group name='[Top clients per region]'>"
    "<groupfilter function='crossjoin'>"
    "<groupfilter count='5' end='top' function='end' units='records'>"
    "<groupfilter direction='DESC' expression='SUM([Sales])' function='order'>"
    "<groupfilter function='level-members' level='[Client]'/>"
    "</groupfilter></groupfilter></groupfilter></group>")


# --------------------------------------------------------------------- the index

def test_a_range_set_is_indexed_with_its_level():
    defs = T._set_definitions(_wb(RANGE_SET))
    sd = defs[("ds1", "Remove Nulls")]
    assert sd["kind"] == "range"
    assert sd["level"] == "Customer Segment"
    assert sd["detail"]["from"] == '"Consumer"'
    assert sd["detail"]["to"] == '"Small Business"'
    assert sd["auto"] is False


def test_a_topn_set_reports_top_n_not_level_members():
    """Kind precedence is load-bearing, not cosmetic.

    A Top-N set nests ``end`` > ``order`` > ``level-members``. A scan that took the LAST or the
    innermost match would classify it as a plain member list and lose the ordering, the direction
    and the count -- i.e. lose everything that makes it a Top-N and everything a future
    translation needs.
    """
    sd = T._set_definitions(_wb(TOPN_SET))[("ds1", "Highest profit customers")]
    assert sd["kind"] == "top-n"
    assert sd["level"] == "Customer Name"
    assert sd["detail"]["end"] == "top"
    assert sd["detail"]["direction"] == "DESC"
    assert sd["detail"]["expression"] == "SUM([Profit])"
    # Tableau's own documented pattern binds the count to a PARAMETER, and PBIR's `Top` is a JSON
    # number with no expression form -- so this attribute decides translatability. Keep it.
    assert sd["detail"]["count"] == "[Parameters].[Parameter 2]"


def test_kind_precedence_is_exercised_by_a_group_matching_TWO_needles():
    """The only test that can fail if the precedence table is reordered.

    Every other fixture matches exactly one needle, so the ordering is unexercised by them -- a
    control that swapped two entries passed, which is how this gap was found. A crossjoin wrapping
    an ``end`` matches BOTH ``crossjoin`` and ``end``, and must resolve to the more specific
    ``top-n``: classifying it as ``crossjoin`` would send a future translation at a plain member
    list and silently discard the ordering, direction and count.
    """
    sd = T._set_definitions(_wb(CROSSJOIN_WRAPPING_END))[("ds1", "Top clients per region")]
    assert sd["kind"] == "top-n", "precedence lost: a crossjoin-wrapped Top-N read as %r" % sd["kind"]
    assert sd["detail"]["end"] == "top"
    assert sd["detail"]["expression"] == "SUM([Sales])"
    # and the table really does list the specific one first
    order = [fn for fn, _ in T._SET_FUNCTION_KINDS]
    assert order.index("end") < order.index("crossjoin")


def test_tableaus_own_dashboard_machinery_is_flagged_auto():
    """26 of the corpus's 56 uniquely-named groups are these. They never reach the resolver, so
    warning on them would report 26 non-defects per build and train the reader to skip the message
    that matters."""
    sd = T._set_definitions(_wb(ACTION_SET))[("ds1", "Action (Region)")]
    assert sd["auto"] is True
    for name in ("Action (Region)", "Tooltip (Show by Dimension)", "Highlight (Segment)"):
        assert T._AUTO_SET_RE.match(name), name
    for name in ("Remove Nulls", "Set 1", "Top Customers by Profit", "Actionable Items"):
        assert not T._AUTO_SET_RE.match(name), name


def test_names_are_stored_unbracketed_so_a_filter_token_can_find_them():
    """Tableau writes the group as ``[Remove Nulls]`` and the filter token resolves to the same
    bracketed form; storing brackets on one side and not the other is exactly the substring trap
    that made an earlier survey report zero shelf references."""
    defs = T._set_definitions(_wb(RANGE_SET))
    assert ("ds1", "Remove Nulls") in defs
    assert ("ds1", "[Remove Nulls]") not in defs


# --------------------------------------------------------------------- the warning

def _warning_for(xml, key):
    sd = T._set_definitions(_wb(xml))[("ds1", key)]
    return T._set_filter_warning(sd, "Solution 01", "ds1")


def test_the_warning_names_the_set_its_kind_its_level_and_the_CONSEQUENCE():
    w = _warning_for(RANGE_SET, "Remove Nulls")
    # Read the exact key rather than a fallback chain: `str(w)` would stringify the whole dict and
    # pass on the machine-readable payload alone, so the prose could be empty and this still green.
    assert sorted(w) == ["dropped_set_filter", "name", "reason", "scope"]
    text = w["reason"]
    assert "Remove Nulls" in text
    assert "range set" in text
    assert "Customer Segment" in text
    # The consequence is the point. A reader who only learns "a filter was dropped" cannot tell
    # whether the visual is missing a column or showing the wrong population.
    assert "SUPERSET" in text
    assert "MORE rows" in text


def test_the_warning_carries_a_machine_readable_marker():
    w = _warning_for(TOPN_SET, "Highest profit customers")
    m = w["dropped_set_filter"]
    assert m["kind"] == "top-n"
    assert m["level"] == "Customer Name"
    assert m["set"] == "Highest profit customers"
    assert m["datasource"] == "ds1"
    assert m["detail"]["count"] == "[Parameters].[Parameter 2]"


# --------------------------------------------------------------------- the worklist

def test_the_worklist_ranks_a_dropped_filter_as_its_own_high_category():
    """Not ``filter`` and not ``field_binding``. Those describe a control that is missing or loosely
    bound; this describes a visual that renders COMPLETE over the wrong population. ``high`` is
    defined in the module as "a data / binding gap that changes what is shown"."""
    reason = ("FILTER DROPPED: Tableau set 'Remove Nulls' (a range set on [Customer Segment]) has "
              "no Power BI equivalent the engine can emit yet, so this visual renders over an "
              "UNFILTERED SUPERSET -- it will look complete and show MORE rows than Tableau")
    cat, sev = RW._classify_warning(reason.lower())
    assert (cat, sev) == ("dropped_filter", "high")
    hint = RW._remediation(cat)
    assert "NOT applied" in hint and "MORE rows" in hint
    assert hint != RW._remediation("other")


def test_the_dropped_filter_rule_outranks_the_looser_filter_rules():
    """Ordered table, first match wins. If a looser ``filter`` rule were reached first this would be
    down-ranked to a generic control gap and lose its severity."""
    names = [r[1] for r in RW._WARNING_RULES]
    assert "dropped_filter" in names
    i = names.index("dropped_filter")
    for later in ("filter", "field_binding"):
        assert i < max(j for j, n in enumerate(names) if n == later), later
