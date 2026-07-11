"""Emit Power BI Q&A linguistic metadata (a model ``cultureInfo``) from Tableau field captions.

Every Tableau ``<column caption='...' name='[...]'>`` carries a curated **caption** -- a human
display name that usually differs from the physical/model column. That caption *is* a natural
Q&A synonym. Turning captions into a Power BI ``cultureInfo`` linguisticMetadata part makes the
migrated model answer natural-language / Copilot questions in Tableau's own vocabulary -- something
neither Power BI's own importer nor the reference tools emit.

Faithful, additive, and OFF by absence: when no field contributes a caption that differs from its
model column name, ``build_linguistic_culture`` returns ``None``, the caller writes no part, and the
model is byte-identical to today.

Byte-shape discipline (whitespace, key order, entity ``State: Generated`` / term ``State:
Suggested`` + ``Weight``, and the deliberately-omitted ``DynamicImprovement``) is locked to three
real Microsoft-generated ``cultureInfo`` files -- see ``files/CULTURE_TMDL_GROUND_TRUTH.md``. This
is the one emission in the Model-Object harvest with no existing in-repo generator, so callers ship
it OPT-IN (default off) until a Power BI Desktop / Tabular Editor round-trip certifies it in the
target environment: a malformed ``cultures/*.tmdl`` fails MODEL LOAD, which would break openability.
"""
import json
import re


def _norm(text):
    """Lowercase and collapse every run of non-alphanumerics to a single ``_`` (Q&A entity key)."""
    return re.sub(r"[^0-9a-z]+", "_", (text or "").lower()).strip("_")


def _entity_key(entity, prop):
    """Normalized ``entity.property`` key (cosmetic -- Power BI rebuilds its own index; the
    load-bearing identity is the ``Binding.ConceptualEntity/ConceptualProperty`` pair)."""
    return "{0}.{1}".format(_norm(entity), _norm(prop))


def _terms_for(model_column, caption):
    """Ordered, de-duplicated Q&A term dicts for a single field.

    Emits the curated caption plus two safe tokenizations (camelCase split, underscore split),
    each lowercased and whitespace-collapsed. A candidate is skipped when it is empty, equals the
    model column name (nothing to add), or was already emitted. Returns ``[]`` when the caption
    contributes no vocabulary -- so a field whose caption matches its column adds no term.
    """
    mc = " ".join((model_column or "").split()).lower()
    terms, seen = [], set()

    def add(candidate):
        term = " ".join((candidate or "").split()).lower()
        if not term or term == mc or term in seen:
            return
        seen.add(term)
        terms.append({term: {"State": "Suggested", "Weight": 0.9}})

    add(caption)                                              # the curated display name
    add(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", caption or ""))  # camelCase -> words
    add((caption or "").replace("_", " "))                   # snake_case -> words
    return terms


def _build_entities(entity_fields):
    """``{entity_key: entity_object}`` for every field that contributes at least one term.

    ``entity_fields`` is an iterable of ``(conceptual_entity, conceptual_property, model_column,
    caption)`` tuples. The first field wins a collided normalized key so output is deterministic.
    """
    entities = {}
    for entity, prop, model_column, caption in entity_fields:
        terms = _terms_for(model_column, caption)
        if not terms:
            continue
        key = _entity_key(entity, prop)
        if key in entities:
            continue
        entities[key] = {
            "Definition": {"Binding": {
                "ConceptualEntity": entity,
                "ConceptualProperty": prop,
            }},
            "State": "Generated",
            "Terms": terms,
        }
    return entities


def build_linguistic_culture(entity_fields, *, language="en-US"):
    """Render a TMDL ``cultureInfo`` part from ``(entity, property, model_column, caption)`` tuples.

    Returns the ``cultureInfo <language>`` TMDL string, or ``None`` when no field contributes a
    term (the caller then writes no part and the model stays byte-identical). ``entity`` is the
    model table (ConceptualEntity), ``property`` the model column name (ConceptualProperty), and
    ``model_column`` the same emitted name used to decide whether ``caption`` adds vocabulary.

    The JSON body uses ``indent=2`` and is re-indented with three leading tabs per line, matching
    the byte-shape captured from real Microsoft ``cultureInfo`` files (see the module docstring).
    """
    entities = _build_entities(entity_fields)
    if not entities:
        return None
    payload = {"Version": "1.0.0", "Language": language, "Entities": entities}
    body = json.dumps(payload, indent=2, ensure_ascii=False)
    indented = "\n".join("\t\t\t" + line for line in body.splitlines())
    return (
        "cultureInfo {0}\n\n".format(language)
        + "\tlinguisticMetadata =\n"
        + indented
        + "\n\t\tcontentType: json\n"
    )


def linguistic_audit(entity_fields, *, language="en-US"):
    """Audit counts for ``report["model_objects"]["linguistic"]`` -- ``{entities, terms, language}``.

    Derived from the SAME ``_build_entities`` pass as ``build_linguistic_culture`` so the report can
    never drift from what was emitted.
    """
    entities = _build_entities(entity_fields)
    terms = sum(len(obj["Terms"]) for obj in entities.values())
    return {"entities": len(entities), "terms": terms, "language": language}
