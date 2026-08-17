"""#138 -- datasource selection must normalise BOTH sides, or a caption can never be matched.

``_choose_datasource`` stripped the REQUESTED name but not the CANDIDATE labels. An asymmetric
normalisation cannot match, so a datasource whose caption carries incidental whitespace -- authored
by hand in Tableau, e.g. the field report's real ``'DS_Visitor _Device '`` -- was unselectable
however correctly it was spelled, and the workbook was skipped outright.

The sharpest statement of the bug is that it broke this module's OWN documented contract:
``workbook_datasources()`` returns ``label`` and documents it as "the value to pass back as
``select=``", and handing that exact string straight back was rejected. The error then rendered the
requested and available names through ``repr``, producing two byte-identical strings above a message
saying one of them did not exist. The reporter's workaround was to edit the customer's ``.twbx``.
"""

import xml.etree.ElementTree as ET

import pytest

import connection_to_m as C

# Caption carries a trailing space, exactly as the field report's workbook did.
UNTRIMMED_CAPTION = "DS_Visitor _Device "

_WB = """<?xml version='1.0' encoding='utf-8'?>
<workbook><datasources>
  <datasource name='federated.abc' caption='%s' version='18.1'>
    <connection class='excel-direct'>
      <named-connections>
        <named-connection caption='x' name='excel.1'>
          <connection class='excel-direct' filename='C:/data/x.xlsx'/>
        </named-connection>
      </named-connections>
      <relation name='Sheet1' table='[Sheet1$]' type='table'/>
    </connection>
  </datasource>
</datasources></workbook>"""


def _wb(caption=UNTRIMMED_CAPTION):
    return _WB % caption


def test_the_label_this_module_hands_out_can_be_passed_straight_back():
    """The documented round-trip: workbook_datasources() -> label -> select=.

    This is the regression that matters most, because an agent following the API exactly hit it.
    """
    listed = C.workbook_datasources(_wb())
    label = listed[0]["label"]
    assert label == UNTRIMMED_CAPTION, "precondition: the label carries the incidental whitespace"

    ds = C._choose_datasource(ET.fromstring(_wb()), select=label)
    assert ds.get("caption") == UNTRIMMED_CAPTION


def test_a_caption_with_incidental_whitespace_matches_the_trimmed_name():
    """What a human would type, having read the name off the screen."""
    ds = C._choose_datasource(ET.fromstring(_wb()), select="DS_Visitor _Device")
    assert ds.get("caption") == UNTRIMMED_CAPTION


@pytest.mark.parametrize("probe", [
    "  DS_Visitor _Device  ",      # whitespace on the REQUESTED side
    "ds_visitor _device",          # case, which already worked -- must keep working
    "DS_Visitor _Device ",         # the caption verbatim
])
def test_selection_is_normalised_symmetrically(probe):
    ds = C._choose_datasource(ET.fromstring(_wb()), select=probe)
    assert ds.get("caption") == UNTRIMMED_CAPTION


def test_internal_name_and_formatted_name_still_select():
    """The other two label sources must not regress -- they are matched the same way."""
    ds = C._choose_datasource(ET.fromstring(_wb()), select="federated.abc")
    assert ds.get("caption") == UNTRIMMED_CAPTION


def test_a_genuinely_absent_name_still_raises():
    """Fail-closed: normalising must not start matching things that are actually different."""
    with pytest.raises(C.AmbiguousDatasourceError):
        C._choose_datasource(ET.fromstring(_wb()), select="Some Other Datasource")


_TWO = """<?xml version='1.0' encoding='utf-8'?>
<workbook><datasources>
  <datasource name='federated.a' caption='DS_Visitor  _Device' version='18.1'>
    <connection class='excel-direct'><relation name='S1' table='[S1$]' type='table'/></connection>
  </datasource>
  <datasource name='federated.b' caption='Totally Different' version='18.1'>
    <connection class='excel-direct'><relation name='S2' table='[S2$]' type='table'/></connection>
  </datasource>
</datasources></workbook>"""


def test_a_near_miss_is_named_in_the_error():
    """Two spaces in the caption, one in the request: report which candidate was close.

    Symmetric stripping does not (and must not) collapse INTERNAL whitespace, so this still fails --
    correctly. What it must not do is print two strings that look alike and leave the reader to
    count spaces.
    """
    with pytest.raises(C.AmbiguousDatasourceError) as ei:
        C._choose_datasource(ET.fromstring(_TWO), select="DS_Visitor _Device")
    msg = str(ei.value)
    assert "closest is" in msg
    assert "differs only in whitespace" in msg
    assert "DS_Visitor  _Device" in msg


def test_no_near_miss_hint_when_nothing_is_close():
    """The hint must not fire on an unrelated name, or it becomes noise."""
    with pytest.raises(C.AmbiguousDatasourceError) as ei:
        C._choose_datasource(ET.fromstring(_TWO), select="Nothing Like It")
    assert "closest is" not in str(ei.value)


def test_the_near_miss_hint_never_selects():
    """Reported, never guessed -- an internal-whitespace near miss still raises rather than binding.

    Collapsing internal whitespace could make two genuinely distinct captions identical, so it is a
    reporting aid only. ``_squash_ws`` must never be reachable from the matching path.
    """
    with pytest.raises(C.AmbiguousDatasourceError):
        C._choose_datasource(ET.fromstring(_TWO), select="DS_Visitor _Device")


def test_squash_ws_is_reporting_only_and_collapses_runs():
    assert C._squash_ws("A  B") == C._squash_ws("A B") == "a b"
    assert C._squash_ws("  Mixed\tCase\nHere ") == "mixed case here"
    assert C._squash_ws(None) == ""
