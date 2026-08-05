"""Dynamic titles and text boxes resolve every token the view PINS.

Tableau weaves live tokens into a worksheet title or a dashboard text zone, and a static Power BI
textbox has no way to evaluate them at render time -- so they have to be resolved at BUILD time or
the reader loses the sentence. Only ``<[Parameters].[id]>`` was ever resolved; a field reference, a
runtime special, and an authored number format were all dropped, which turned an authored status
band into a scaffold with holes in it.

Measured on a customer workbook built deliberately to be hard (40 parameters, seven of them sharing
the caption ``Select Metric Rank - Network...``, so nothing can be resolved by caption):

    source   Sort By = <[Parameters].[Parameter 2 2]> | Region =Æ <[ds].[none:Region:nk]>
             | Fiscal Month =<[ds].[none:FiscalMonth:ok]> <[Parameters].[Parameter 3 1 1]>
             <[Parameters].[New Quota]><Page Name><Workbook Name><Data Update Time>
             <[ds].[none:FiscalMonth:ok]>

    before   Sort By = % Multi-Home (MH) | Region =Æ | Fiscal Month = HQ500000
    after    Sort By = % Multi-Home (MH) | Region = Big South | Fiscal Month =6/21/2026
             HQ$500KDyanmic Titles and Text Boxes7/24/2026 11:33:40 AM6/21/2026
    Tableau  ... identical, except it renders the refresh stamp 4:33:40 AM (viewer-local).

Every rule below is read from the workbook, never inferred from a name:

  * a PARAMETER resolves by internal id to its alias, else its authored number format, else its
    raw literal -- so ``0`` renders ``HQ`` and ``500000`` renders ``$500K``;
  * a FIELD resolves to the value this view pins it to: the single member its filter selects
    (``Big South``), or ``All`` when the filter is unrestricted. Both are confirmed against source
    renders of two independent customer workbooks. A selection of SEVERAL specific members is left
    unresolved -- the observed evidence is ambiguous there, and a wrong literal in a header is worse
    than a blank one;
  * RUNTIME SPECIALS resolve from the file: workbook name, sheet name, and the extract's recorded
    refresh stamp. ``<Page Name>`` resolves to empty because Tableau itself renders nothing for it
    on a view with no Pages shelf (confirmed: five of six tokens in that run produced text);
  * anything else FAILS CLOSED -- a bare ``<Region>`` or a viewer-dependent ``<User Name>`` is
    reported unresolved so a chart title declines rather than leaking raw markup.
"""
import twb_to_pbir as R


# -- the authored number format --------------------------------------------------------------

def test_thousands_scaling_with_currency_and_suffix():
    """The customer's `$500K`: a trailing comma scales by 1000 and `K` is a literal."""
    assert R._format_number_literal(500000, 'c"$"#,##0,K;("$"#,##0,K)') == "$500K"


def test_double_comma_scales_by_a_million():
    assert R._format_number_literal(2500000, 'c"$"#,##0,,M') == "$3M"


def test_rounding_is_half_away_from_zero_like_excel():
    """Python rounds half to EVEN, so 2.5 would render '2' and disagree with the source."""
    assert R._format_number_literal(2500000, 'c#,##0,,') == "3"


def test_negative_uses_the_second_section():
    assert R._format_number_literal(-500000, 'c"$"#,##0,K;("$"#,##0,K)') == "($500K)"


def test_grouping_and_decimals():
    assert R._format_number_literal(1234.56, "c#,##0.00") == "1,234.56"
    assert R._format_number_literal(1234, 'c"$"#,##0;("$"#,##0)') == "$1,234"


def test_percent_precision_form():
    assert R._format_number_literal(0.184, "p1%") == "18.4%"


def test_ambiguous_precision_code_declines():
    """`n2` does not say whether the author wanted grouping -- decline beats guessing."""
    assert R._format_number_literal(1234.5, "n2") is None


def test_non_numeric_and_missing_code_decline():
    assert R._format_number_literal("Network Score", "c#,##0") is None
    assert R._format_number_literal(100, None) is None
    assert R._format_number_literal(None, "c#,##0") is None


# -- filter member display --------------------------------------------------------------------

def test_string_member_loses_only_its_serialisation_quotes():
    assert R._filter_member_display('"Big South"') == "Big South"


def test_date_member_renders_in_tableau_short_form():
    assert R._filter_member_display("#2026-06-21#") == "6/21/2026"
    assert R._filter_member_display("#2026-01-05#") == "1/5/2026"


# -- token substitution -------------------------------------------------------------------------

_PARAMS = {
    "Parameter 2 2": {"current_display": "% Multi-Home (MH)"},
    "Parameter 3 1 1": {"current_display": "HQ"},
    "New Quota": {"current_display": "$500K"},
}
_FIELDS = {
    "[ds].[none:Region:nk]": "Big South",
    "[ds].[none:FiscalMonth:ok]": "6/21/2026",
}
_SPECIALS = {"Sheet Name": "filters", "Workbook Name": "WB", "Data Update Time": "7/24/2026",
             "Page Name": ""}


def _sub(text):
    return R._substitute_dynamic_tokens(text, _PARAMS, _FIELDS, _SPECIALS)


def test_parameter_field_and_special_tokens_all_resolve():
    out, unresolved = _sub(
        "Sort By = <[Parameters].[Parameter 2 2]> | Region = <[ds].[none:Region:nk]> "
        "| <[Parameters].[New Quota]><Workbook Name>")
    assert out == "Sort By = % Multi-Home (MH) | Region = Big South | $500KWB"
    assert unresolved == []


def test_page_name_resolves_empty_without_vetoing_the_title():
    """Tableau renders nothing for it on a view with no Pages shelf -- so neither do we, quietly."""
    out, unresolved = _sub("A<Page Name>B")
    assert out == "AB"
    assert unresolved == []


def test_an_unpinned_field_is_reported_unresolved():
    out, unresolved = _sub("Value = <[ds].[none:Unpinned:nk]>")
    assert unresolved == ["<[ds].[none:Unpinned:nk]>"]


def test_a_bare_token_fails_closed_instead_of_leaking_markup():
    """The regression guard: an unrecognised shape must not survive as raw text."""
    out, unresolved = _sub("Sales for <Region>")
    assert unresolved == ["<Region>"]


def test_a_viewer_dependent_special_is_not_invented():
    out, unresolved = _sub("Hello <User Name>")
    assert unresolved == ["<User Name>"]


# -- the two call-site policies -----------------------------------------------------------------

def test_a_chart_title_declines_when_anything_is_unresolved():
    assert R._resolve_dynamic_title(
        "Sales for <[ds].[none:Unpinned:nk]>", _PARAMS, _FIELDS, _SPECIALS) is None


def test_a_chart_title_is_kept_when_fully_resolved():
    assert R._resolve_dynamic_title(
        "Sales for <[ds].[none:Region:nk]>", _PARAMS, _FIELDS, _SPECIALS) == "Sales for Big South"


def test_a_status_band_blanks_what_it_cannot_resolve_but_keeps_the_scaffold():
    text = R._resolve_caption_text(
        "Region = <[ds].[none:Region:nk]> | Other = <[ds].[none:Unpinned:nk]>",
        _PARAMS, _FIELDS, _SPECIALS)
    assert text == "Region = Big South | Other ="
    assert "<" not in text and ">" not in text


# -- the Æ line-break sentinel vs the real letter -------------------------------------------------

def test_a_run_that_is_only_the_sentinel_is_scrubbed_but_keeps_its_spacing():
    ws = _title_ws("<run>Region =</run><run>\u00c6 </run><run>Big South</run>")
    assert R._parse_worksheet_title(ws)[0] == "Region = Big South"


def test_a_sentinel_before_a_hard_break_is_scrubbed():
    ws = _title_ws("<run>New Inbound\u00c6\nReferrals</run>")
    assert "\u00c6" not in R._parse_worksheet_title(ws)[0]


def test_a_real_ae_letter_inside_a_word_survives():
    """Over-scrub guard: `Ærø` is Danish text, not a layout marker."""
    ws = _title_ws("<run>\u00c6r\u00f8 Sales</run>")
    assert R._parse_worksheet_title(ws)[0] == "\u00c6r\u00f8 Sales"


def _title_ws(runs_xml):
    import xml.etree.ElementTree as ET
    return ET.fromstring(
        "<worksheet name='W'><layout-options><title><formatted-text>%s"
        "</formatted-text></title></layout-options></worksheet>" % runs_xml)
