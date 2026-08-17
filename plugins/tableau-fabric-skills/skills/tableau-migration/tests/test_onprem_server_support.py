"""#140 -- on-prem Tableau Server is supported deliberately, not incidentally.

The question asked was not "does it work" (a live on-prem run was reported working) but *"is Server
in your test matrix, or does it work by accident?"*. Before this module the honest answer was
"by construction, but untested": every automated check used a Cloud pod string, so nothing would
have caught an on-prem regression.

These tests cover the parts that are genuinely checkable offline -- host-shape handling and REST URL
construction, which is the whole of what differs between a Cloud pod and an on-prem host, since the
endpoints themselves are identical. What they deliberately do NOT claim to cover is a live on-prem
round trip against a real Server.

The remaining Server/Cloud divergence is version pinning, and it is not negotiated:
``DEFAULT_REST_VERSION`` is a constant and no ``serverinfo`` call is ever made. An on-prem Server can
run a substantially older REST API than Cloud ever does, so ``--rest-version`` is the documented
mitigation -- and ``test_an_older_rest_version_is_honoured`` pins that it actually reaches the URL,
because a documented flag that silently did nothing would be worse than no flag at all.
"""

import pytest

import fetch_tds as F

CLOUD = "10ay.online.tableau.com"
ONPREM = "https://tableau.example.com"


@pytest.mark.parametrize("given,expected", [
    (CLOUD, "https://10ay.online.tableau.com"),          # Cloud pod, scheme implied
    (ONPREM, "https://tableau.example.com"),             # on-prem, scheme explicit
    ("tableau.example.com", "https://tableau.example.com"),   # on-prem, bare host
    ("https://tableau.example.com/", "https://tableau.example.com"),  # trailing slash
    ("http://tab.internal:8000", "http://tab.internal:8000"),  # on-prem, plain http + port
])
def test_on_prem_and_cloud_hosts_normalise_the_same_way(given, expected):
    assert F.normalize_server(given) == expected


def test_an_on_prem_host_keeps_its_scheme_and_port():
    """An internal Server is frequently plain http on a non-default port.

    Silently upgrading it to https would make every request fail against a host that never served
    TLS, so this pins that the scheme is preserved rather than assumed.
    """
    base = F.rest_base("http://tab.internal:8000", F.DEFAULT_REST_VERSION)
    assert base == "http://tab.internal:8000/api/%s" % F.DEFAULT_REST_VERSION


def test_rest_urls_have_the_same_shape_on_server_and_cloud():
    """The REST surface is host-agnostic -- this is WHY Server works, made checkable."""
    for host in (CLOUD, ONPREM):
        base = F.rest_base(host, "3.24")
        assert base.endswith("/api/3.24")
        ds = F.datasources_url(host, "3.24", "site-id")
        assert "/api/3.24/sites/site-id/datasources" in ds
        wb = F.workbooks_url(host, "3.24", "site-id")
        assert "/api/3.24/sites/site-id/workbooks" in wb


def test_an_older_rest_version_is_honoured():
    """The documented mitigation for an older on-prem Server must actually reach the URL.

    There is no negotiation -- no ``serverinfo`` call is made and the default is a constant -- so
    ``--rest-version`` is the only lever a user has. A flag that silently did nothing would be worse
    than no flag, because it would send them looking at auth instead.
    """
    assert F.rest_base(ONPREM, "2.8") == "https://tableau.example.com/api/2.8"
    assert "/api/2.8/sites/s/datasources" in F.datasources_url(ONPREM, "2.8", "s")


def test_the_default_rest_version_is_a_pinned_constant_not_negotiated():
    """Pins the documented fact, so the docstring cannot quietly become untrue.

    If negotiation is ever added, this test should be REPLACED by one covering it -- the point is
    that the module's claim about itself stays checkable either way.
    """
    assert isinstance(F.DEFAULT_REST_VERSION, str)
    assert F.DEFAULT_REST_VERSION.replace(".", "").isdigit()
    src = open(F.__file__, encoding="utf-8-sig").read().lower()
    assert "serverinfo" not in src, (
        "a serverinfo call now exists -- version IS negotiated, so update the Server/Cloud note in "
        "estate_survey's docstring and replace this test")
