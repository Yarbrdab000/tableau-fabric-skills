"""Calc dependency graph, roots-first topological ordering, and conservative
row-level form inference.

Tableau calculated fields form a dependency graph: a calc references physical
columns, parameters, and *other calcs*. On the corpus workbooks that graph is a
clean DAG several layers deep, and the single biggest migration failure is
building a calc before the calc it depends on exists -- "installing a door before
the wall". This module lays the foundation for a roots-first resolver by
answering three structural questions, WITHOUT emitting any DAX:

  * ``build_calc_graph``    -- what does each calc depend on? (calc / parameter /
    physical column, references resolved by BOTH caption and Tableau internal
    name, exactly as the model build keys them).
  * ``topological_layers``  -- in what ORDER must calcs be built so no calc is
    built before its dependencies? (roots first; a dependency cycle is tolerated
    -- its members are returned unresolved, never mis-ordered).
  * ``infer_row_level``     -- is a calc faithfully ROW-LEVEL (a DAX calculated
    column) or AGGREGATE (a measure)? Tableau forbids mixing the two in one
    expression, so this is a clean dichotomy: a calc is row-level iff it has no
    aggregation / LOD / table-calc at its own level AND every calc it references
    is itself row-level. Conservative and fail-closed -- anything uncertain
    (an LOD, a cycle, an aggregate dependency) is reported NOT row-level, so a
    caller that reroutes only row-level calcs never wrongly relocates a measure.

Pure stdlib; reuses the calc tokenizer so a field literally named like a
function (``[Count of Contacts]``) is never mistaken for an aggregation, and a
bracket inside a string literal never corrupts reference extraction.
"""

try:  # dual import: installed as a package, or scripts/ dropped on sys.path
    from .calc_to_dax import _tokenize, _CalcError
except ImportError:  # pragma: no cover - exercised via the standalone path
    from calc_to_dax import _tokenize, _CalcError


# Aggregation functions: an ``id`` token matching one of these (a bare, unbracketed
# identifier) marks the expression aggregate. A field is always bracketed, so a
# field named "Count of Contacts" tokenizes as ("field", ...) and never matches.
_AGG_FUNCS = frozenset({
    "SUM", "AVG", "AVERAGE", "MIN", "MAX", "MEDIAN", "COUNT", "COUNTD",
    "ATTR", "STDEV", "STDEVP", "VAR", "VARP", "PERCENTILE",
    "CORR", "COVAR", "COVARP",
})

# Table-calculation functions (view-context; never a row-level column).
_TABLECALC_FUNCS = frozenset({
    "INDEX", "SIZE", "FIRST", "LAST", "RANK", "RANK_DENSE", "RANK_MODIFIED",
    "RANK_PERCENTILE", "RANK_UNIQUE", "LOOKUP", "PREVIOUS_VALUE", "TOTAL",
    "RUNNING_SUM", "RUNNING_AVG", "RUNNING_COUNT", "RUNNING_MAX", "RUNNING_MIN",
    "WINDOW_SUM", "WINDOW_AVG", "WINDOW_MEDIAN", "WINDOW_COUNT", "WINDOW_MAX",
    "WINDOW_MIN", "WINDOW_STDEV", "WINDOW_STDEVP", "WINDOW_VAR", "WINDOW_VARP",
    "WINDOW_PERCENTILE", "WINDOW_CORR", "WINDOW_COVAR", "WINDOW_COVARP",
})
_TABLECALC_PREFIXES = ("WINDOW_", "RUNNING_", "RANK_")


def calc_key(calc):
    """Canonical graph key for a calc: its Tableau internal name if present, else
    its caption -- lower-cased and stripped. Matches how the model build keys the
    ``measure_refs`` / ``column_refs`` registries so the two never drift."""
    return str(calc.get("internal_name") or calc.get("name") or "").strip().lower()


def _ids(toks):
    return [v.upper() for (k, v) in toks if k == "id"]


def _has_aggregation(toks):
    return any(i in _AGG_FUNCS for i in _ids(toks))


def _has_lod(toks):
    # A Level-Of-Detail expression is delimited by braces; the tokenizer emits
    # ("op", "{"). (A '{' never appears elsewhere in Tableau calc syntax.)
    return any(k == "op" and v == "{" for (k, v) in toks)


def _has_tablecalc(toks):
    for i in _ids(toks):
        if i in _TABLECALC_FUNCS or any(i.startswith(p) for p in _TABLECALC_PREFIXES):
            return True
    return False


def _tokens(formula):
    try:
        return _tokenize(formula or "")
    except _CalcError:
        return None  # untokenizable -> caller treats as opaque (no deps, not row-level)


def build_calc_graph(calcs):
    """Build the dependency graph over ``calcs`` (each ``{name, formula,
    internal_name?, role?}``).

    Returns a :class:`CalcGraph` whose ``nodes`` maps each calc's :func:`calc_key`
    to a node dict:
        ``{name, internal_name, role, formula, key, calc_deps, param_refs,
           phys_refs, has_aggregation, has_lod, has_tablecalc}``
    where ``calc_deps`` is the set of OTHER calc keys this calc references (self
    references dropped), ``param_refs`` the referenced parameter names, and
    ``phys_refs`` every remaining ``[field]`` (a physical column, or an
    unresolved reference). References resolve by BOTH caption and internal name.
    """
    # alias map: caption AND internal name (both lower-cased) -> canonical key
    alias = {}
    order = []
    for c in calcs or []:
        k = calc_key(c)
        if not k:
            continue
        order.append(k)
        nm = (c.get("name") or "").strip().lower()
        inm = str(c.get("internal_name") or "").strip().lower()
        if nm:
            alias.setdefault(nm, k)
        if inm:
            alias.setdefault(inm, k)

    nodes = {}
    for c in calcs or []:
        k = calc_key(c)
        if not k or k in nodes:
            continue
        toks = _tokens(c.get("formula", ""))
        calc_deps, param_refs, phys_refs = set(), set(), set()
        if toks is not None:
            for (kind, val) in toks:
                if kind == "field":
                    t = (val or "").strip().lower()
                    dep = alias.get(t)
                    if dep is not None and dep != k:
                        calc_deps.add(dep)
                    elif dep is None:
                        phys_refs.add(t)
                    # dep == k (self-reference) is dropped
                elif kind == "qfield":
                    parts = val or []
                    head = (parts[0] if parts else "").strip().lower()
                    tail = (parts[-1] if parts else "").strip().lower()
                    if head == "parameters":
                        if tail:
                            param_refs.add(tail)
                    elif tail:
                        # datasource-qualified / blend field: treat as physical
                        # (cross-datasource calc references are out of scope).
                        phys_refs.add(tail)
        nodes[k] = {
            "name": c.get("name"),
            "internal_name": c.get("internal_name"),
            "role": (c.get("role") or "measure").lower(),
            "formula": c.get("formula", ""),
            "key": k,
            "calc_deps": calc_deps,
            "param_refs": param_refs,
            "phys_refs": phys_refs,
            "has_aggregation": bool(toks is not None and _has_aggregation(toks)),
            "has_lod": bool(toks is not None and _has_lod(toks)),
            "has_tablecalc": bool(toks is not None and _has_tablecalc(toks)),
        }
    return CalcGraph(nodes, alias, order)


class CalcGraph:
    """Immutable view of the calc dependency graph. ``nodes`` is keyed by
    :func:`calc_key`; ``alias`` maps every caption/internal-name to its key;
    ``order`` preserves input order (used as a stable tie-break in layering)."""

    __slots__ = ("nodes", "alias", "order")

    def __init__(self, nodes, alias, order):
        self.nodes = nodes
        self.alias = alias
        self.order = order

    def deps(self, key):
        """Calc dependencies of ``key`` restricted to keys that are real nodes."""
        node = self.nodes.get(key)
        if not node:
            return set()
        return {d for d in node["calc_deps"] if d in self.nodes}


def topological_layers(graph):
    """Roots-first topological layering of ``graph``.

    Returns ``(layers, unresolved)``: ``layers[0]`` is every calc that depends on
    no other calc (roots -- physical/parameter-only), ``layers[i+1]`` every calc
    whose calc dependencies are all in layers ``0..i``. ``unresolved`` lists calcs
    that never place because they sit on a dependency cycle (fail-closed: they are
    omitted from ``layers`` rather than mis-ordered). Input order breaks ties
    within a layer so the output is deterministic.
    """
    placed = set()
    layers = []
    remaining = set(graph.nodes)
    while remaining:
        ready = [k for k in graph.order
                 if k in remaining and graph.deps(k) <= placed]
        if not ready:
            break  # everything left is on a cycle
        layers.append(ready)
        placed.update(ready)
        remaining.difference_update(ready)
    unresolved = [k for k in graph.order if k in remaining]
    return layers, unresolved


def infer_row_level(graph):
    """Conservative row-level form inference over ``graph``.

    Returns ``{key -> bool}``: ``True`` iff the calc is faithfully a ROW-LEVEL
    calculated column, ``False`` iff it is aggregate / uncertain (a measure, an
    LOD, a table calc, a cycle member, or anything referencing a non-row-level
    calc). Resolved roots-first so a calc's decision always sees its
    dependencies' decisions. The dichotomy matches Tableau: an expression is
    aggregate if ANY part of it is aggregate, so a calc is row-level only when it
    is row-level "all the way down".
    """
    forms = {}
    layers, unresolved = topological_layers(graph)
    for k in unresolved:
        forms[k] = False  # a cycle can't be confirmed row-level
    for layer in layers:
        for k in layer:
            node = graph.nodes[k]
            row_level = (
                not node["has_aggregation"]
                and not node["has_lod"]
                and not node["has_tablecalc"]
                and all(forms.get(d) is True for d in graph.deps(k))
            )
            forms[k] = bool(row_level)
    return forms
