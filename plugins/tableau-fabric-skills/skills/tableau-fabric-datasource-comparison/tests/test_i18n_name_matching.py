"""i18n name matching (#5): non-ASCII names must contribute to the name signal.

``normalize_token`` used to fold on ``[^a-z0-9]+``, which strips **all** non-ASCII. A name written
only in a non-Latin script normalised to ``""``, and since :func:`name_similarity`'s exact-match
short-circuit requires a non-empty normalised form (``na and na == nb``), two *identical* CJK /
Cyrillic / Greek / Arabic names scored **0.0**. That under-counts "already exists" for international
estates and inflates "needs rebuild" -- the opposite of the skill's purpose.

The two halves locked here: non-ASCII names now match, and **ASCII scoring is unchanged**.
"""
import os
import re
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
sys.path.insert(0, HERE)

import compare as C  # noqa: E402


# --- the pre-fix implementation, verbatim, as a differential oracle -------------------------------
_OLD_SPLIT = re.compile(r"[^a-z0-9]+")


def _old_normalize(value):
    if value is None:
        return ""
    return _OLD_SPLIT.sub("", str(value).lower())


def _old_tokenize(value):
    if not value:
        return set()
    toks = {t for t in _OLD_SPLIT.split(str(value).lower()) if t}
    meaningful = {t for t in toks if t not in C._NAME_STOPWORDS}
    return meaningful or toks


_ASCII_NAMES = [
    "Sales Amount", "[Net_Bookings USD]", "Superstore - Extract (v2)", "ORDERS.dbo.Fact_Sales",
    "the model of data", "2024 Q1 Revenue", "a_b-c.d e", "", "   ", "___", "v1", "COPY of COPY",
    "Orders", "orders", "ORDERS", "net_bookings_usd", "Fact/Sales$", "x", "1", "customer id",
    "The Data Source Model", "semantic dataset live extract",
]


# --- (a) ASCII behaviour is byte-identical -------------------------------------------------------
def test_ascii_normalize_token_is_unchanged():
    for name in _ASCII_NAMES:
        assert C.normalize_token(name) == _old_normalize(name), name


def test_ascii_tokenize_name_is_unchanged():
    for name in _ASCII_NAMES:
        assert C.tokenize_name(name) == _old_tokenize(name), name


def test_ascii_normalization_is_unchanged_over_a_large_random_sweep():
    """Randomised differential: no printable-ASCII name may normalise differently than before."""
    import random
    rng = random.Random(7)
    alphabet = ("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
                " _-[]().,/$%#@!&*+='\"<>:;|\\~`^{}?")
    for _ in range(3000):
        name = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 28)))
        assert C.normalize_token(name) == _old_normalize(name), repr(name)
        assert C.tokenize_name(name) == _old_tokenize(name), repr(name)


def test_identical_ascii_names_still_score_exactly_one():
    for name in ("Superstore", "Net Bookings USD", "orders.dbo.fact"):
        assert C.name_similarity(name, name) == 1.0


def test_unrelated_ascii_names_still_score_low():
    assert C.name_similarity("Superstore Sales", "HR Headcount") < 0.35


# --- (b) THE BUG: identical non-ASCII names scored 0.0 -------------------------------------------
_IDENTICAL = [
    ("Japanese", "売上"),
    ("Japanese phrase", "売上分析ダッシュボード"),
    ("Chinese", "销售数据"),
    ("Korean", "매출 분석"),
    ("Cyrillic", "Продажи"),
    ("Greek", "Πωλήσεις"),
    ("Arabic", "المبيعات"),
    ("Hebrew", "מכירות"),
    ("Thai", "ยอดขาย"),
]


def test_identical_non_ascii_names_now_score_an_exact_match():
    for label, name in _IDENTICAL:
        assert _old_normalize(name) == "", f"{label}: fixture must exercise the old bug"
        assert C.normalize_token(name) != "", label
        assert C.name_similarity(name, name) == 1.0, label


def test_identical_non_ascii_names_land_in_the_exact_band():
    """Acceptance: name-only match, no column/source overlap, must reach the top band."""
    for label, name in _IDENTICAL:
        assert C.name_similarity(name, name) == 1.0, label


def test_different_non_ascii_names_do_not_collide():
    for a, b in (("売上", "費用"), ("Продажи", "Расходы"), ("销售数据", "库存数据"),
                 ("Πωλήσεις", "Έξοδα")):
        assert C.name_similarity(a, b) < 1.0, (a, b)


def test_non_ascii_tokens_survive_tokenization():
    assert C.tokenize_name("매출 분석") == {"매출", "분석"}
    assert C.tokenize_name("Продажи по регионам") == {"продажи", "по", "регионам"}


def test_mixed_script_names_match():
    assert C.name_similarity("売上 Dashboard", "売上 Dashboard") == 1.0
    assert C.normalize_token("売上 Dashboard") == "売上dashboard"


# --- (c) case and diacritic folding ---------------------------------------------------------------
def test_diacritics_fold_so_accented_and_plain_names_match():
    """`Café` used to normalise to `caf` (the accent stripped), which is neither name."""
    for a, b in (("Café", "Cafe"), ("Ümsatz", "Umsatz"), ("naïve", "naive"), ("Ventás", "Ventas")):
        assert C.name_similarity(a, b) == 1.0, (a, b)


def test_non_ascii_case_folds():
    assert C.normalize_token("ПРОДАЖИ") == C.normalize_token("продажи")
    assert C.normalize_token("ΠΩΛΗΣΕΙΣ") == C.normalize_token("πωλήσεις")


def test_casefold_not_lower_so_sharp_s_matches_its_expansion():
    """`str.lower()` leaves `ß` alone; `str.casefold()` expands it to `ss`.

    A German estate naming the same asset `Straße Umsatz` and `Strasse Umsatz` must match. This is
    the case that distinguishes casefold from lower -- without it, reverting to lower is invisible.
    """
    assert C.normalize_token("Straße") == "strasse"
    assert C.name_similarity("Straße Umsatz", "Strasse Umsatz") == 1.0


def test_fullwidth_forms_fold_to_their_ascii_equivalents():
    """NFKD maps the fullwidth Latin block, so `Ｓａｌｅｓ` and `Sales` are one name."""
    assert C.name_similarity("Ｓａｌｅｓ", "Sales") == 1.0


def test_hangul_syllables_are_recomposed_not_shattered_into_jamo():
    """NFKD alone decomposes 한 -> 3 jamo; the NFC recomposition step puts it back."""
    assert C.normalize_token("한국") == "한국"
    assert len(C.normalize_token("한국")) == 2


# --- (d) the downstream signals that also discarded empty tokens ---------------------------------
def test_non_ascii_column_names_are_no_longer_discarded():
    tokens = {C.normalize_token(n) for n in ("売上", "顧客", "地域")}
    assert "" not in tokens
    assert len(tokens) == 3


def test_non_ascii_column_overlap_scores():
    """The column signal discarded empty-normalised names, so non-ASCII columns never overlapped."""
    fields = [{"name": "売上", "type": "real"}, {"name": "顧客", "type": "string"},
              {"name": "地域", "type": "string"}]
    cols = [{"name": "売上", "type": "double"}, {"name": "顧客", "type": "string"},
            {"name": "地域", "type": "string"}]
    res = C.score_pair({"name": "売上分析", "fields": fields},
                       {"name": "売上分析", "columns": cols})
    assert res["signals"]["column"] == 1.0
    assert res["signals"]["name"] == 1.0


def test_non_ascii_columns_that_differ_do_not_score_full():
    res = C.score_pair(
        {"name": "売上", "fields": [{"name": "売上"}, {"name": "顧客"}]},
        {"name": "費用", "columns": [{"name": "費用"}, {"name": "在庫"}]})
    assert res["signals"]["column"] < 1.0
    assert res["signals"]["name"] < 1.0


def test_a_name_only_non_ascii_match_reaches_the_exact_band_end_to_end():
    """Acceptance criterion: identical non-ASCII names, NO column/source overlap -> band Exact.

    Before the fix the name signal contributed 0.0 for these, so an international estate could only
    match on columns + sources and a name-only match failed outright.
    """
    res = C.score_pair({"name": "売上分析ダッシュボード", "fields": [], "sources": []},
                       {"name": "売上分析ダッシュボード", "columns": [], "sources": []})
    assert res["signals"]["name"] == 1.0
    assert C.name_similarity("売上分析ダッシュボード", "売上分析ダッシュボード") == 1.0


# --- (e) robustness --------------------------------------------------------------------------------
def test_none_and_empty_are_still_safe():
    assert C.normalize_token(None) == ""
    assert C.normalize_token("") == ""
    assert C.tokenize_name(None) == set()
    assert C.tokenize_name("") == set()
    assert C.name_similarity(None, None) == 0.0
    assert C.name_similarity("", "") == 0.0


def test_punctuation_only_non_ascii_name_is_still_empty():
    """A name of pure symbols has no alphanumerics in ANY script -- it must stay empty, not match."""
    assert C.normalize_token("---") == ""
    assert C.normalize_token("※★→") == ""
    assert C.name_similarity("※★→", "※★→") == 0.0


def test_emoji_only_names_do_not_manufacture_a_match():
    assert C.normalize_token("📊📈") == ""
    assert C.name_similarity("📊📈", "📊📈") == 0.0


def test_a_name_that_is_only_stopwords_still_returns_tokens():
    assert C.tokenize_name("the data source") == {"the", "data", "source"}
