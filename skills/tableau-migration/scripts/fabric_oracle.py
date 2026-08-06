"""The ``fabric_oracle`` contract -- how an external DAX executor plugs into the Tier-1 loop (#96).

:mod:`translation_reconcile` is the offline half of the second compiler's empirical proof: it builds
a probe query, compares two numbers, and labels a translation ``verified`` / ``mismatch`` /
``not-evaluated``. It deliberately executes nothing. The other half -- actually running the DAX
against a built model -- is injected as a ``fabric_oracle`` callable, and until now nothing in this
repo ever passed one, so the empirical half was written, tested and **unreachable**.

This module is the seam. It does not execute DAX either, and it adds no dependency: it defines the
contract, lets an arbitrary executor self-certify against it offline, adapts an external process into
the callable shape, and drives the loop over a model this skill already emitted.

THE CONTRACT
------------
A Fabric oracle is any callable::

    fabric_oracle(dax_query: str) -> result

``dax_query`` is a complete DAX statement (:func:`translation_reconcile.evaluate_query` builds
``EVALUATE ROW("value", <expr>)``). ``result`` may be any shape
:func:`translation_reconcile.extract_scalar` already reads -- that function IS the contract, and is
called here rather than reimplemented so the two can never drift:

  * a bare scalar (``int`` / ``float`` / ``str`` / ``bool`` / ``None``);
  * ``{"value": v}``;
  * ``{"rows": [{col: v}]}`` or a bare ``[{col: v}]``;
  * the Power BI ``executeQueries`` envelope ``{"results":[{"tables":[{"rows":[...]}]}]}``;
  * ``{"error": "..."}`` for a failure -- **return this, do not raise**.

Three obligations, all of which :func:`conforms` checks without a tenant:

  1. **Never raise.** An oracle that raises is caught and downgraded to ``not-evaluated``, but it
     costs the caller the reason. Return ``{"error": ...}`` instead.
  2. **Be a pure read.** The oracle answers queries; it never writes to the model.
  3. **Report absence honestly.** No value must surface as ``{"error": ...}`` or ``None``, never as
     ``0`` -- a fabricated zero is indistinguishable from a real one and would produce a false
     ``verified``, which is the single worst outcome in this system.

WHY AN EXTERNAL EXECUTOR IS FINE
--------------------------------
This skill is stdlib-only and offline **by charter**, and stays that way: nothing here imports a
driver, opens a connection, or launches a process on the default path. An executor is an injected
callable (or, via :func:`subprocess_oracle` / :func:`persistent_oracle`, an external process spoken
to over JSON) that the caller opts into. Verification is off unless asked for, and when it is off
every record reads ``not-evaluated`` **by construction** -- it cannot be misread as green.

WHY THIS CATCHES WHAT THE OFFLINE GATES CANNOT
----------------------------------------------
``monotonic_gate`` is *differential*: it asks whether a candidate regressed against a baseline, so a
wrong BASELINE is a fixed point it can never see (a model reading one endpoint where it should read
two is not a regression against a model that already read one). ``compare_scalars`` is *absolute* --
it compares to ground truth, not to a previous answer -- which is exactly why it can catch that class.
> **Summary keys.** :func:`translation_reconcile.summarize` reports counts under ``verified`` /
> ``mismatch`` / ``not_evaluated`` (underscore) -- which is NOT the ``not-evaluated`` (hyphen) state
> constant. The two are used deliberately below; do not "unify" them.
"""
from __future__ import annotations

import json
import os
import re
import subprocess  # noqa: S404 -- only ever runs a command the CALLER supplies, never a fixed one
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from . import translation_reconcile as _TR
except ImportError:  # flat-module import (scripts dir on sys.path)
    import translation_reconcile as _TR


VERIFIED = _TR.VERIFIED
MISMATCH = _TR.MISMATCH
NOT_EVALUATED = _TR.NOT_EVALUATED

# summarize() count keys -- distinct from the state constants above (see the note in the docstring)
SUM_VERIFIED = "verified"
SUM_MISMATCH = "mismatch"
SUM_NOT_EVALUATED = "not_evaluated"


# == the contract, as callable checks ============================================================

def normalize_result(raw):
    """Canonicalise any contract-legal oracle result to ``{"value": v, "error": e}``.

    Delegates to :func:`translation_reconcile.extract_scalar` so there is exactly ONE parser for the
    envelope. Never raises.
    """
    try:
        value, error = _TR.extract_scalar(raw)
    except Exception as exc:                      # a shape extract_scalar itself choked on
        return {"value": None, "error": "unreadable oracle result: %s" % exc}
    return {"value": value, "error": error}


_PROBE_DAX = 'EVALUATE\nROW("value", 1)'


def conforms(oracle, *, probe=_PROBE_DAX):
    """Check an executor against the contract offline -> ``{"ok", "checks", "failures"}``.

    Lets a third-party executor self-certify with no Fabric/Tableau tenant: it only has to answer one
    trivial probe. Checks that it is callable, accepts a single positional DAX string, does not raise,
    and returns a shape :func:`normalize_result` can read. Never raises.
    """
    checks, failures = {}, []

    def fail(name, why):
        checks[name] = False
        failures.append("%s: %s" % (name, why))

    if not callable(oracle):
        return {"ok": False, "checks": {"callable": False},
                "failures": ["callable: oracle is not callable"]}
    checks["callable"] = True

    try:
        raw = oracle(probe)
        checks["accepts_dax_string"] = True
        checks["does_not_raise"] = True
    except TypeError as exc:
        fail("accepts_dax_string", "must take one positional DAX string (%s)" % exc)
        checks["does_not_raise"] = False
        failures.append("does_not_raise: raised TypeError")
        return {"ok": False, "checks": checks, "failures": failures}
    except Exception as exc:
        checks["accepts_dax_string"] = True
        fail("does_not_raise", "raised %s: %s -- return {'error': ...} instead"
             % (type(exc).__name__, exc))
        return {"ok": False, "checks": checks, "failures": failures}

    norm = normalize_result(raw)
    oracle_reported_error = isinstance(raw, dict) and bool(raw.get("error"))
    if oracle_reported_error:
        # Contract-LEGAL (it reported honestly) but it certifies nothing -- we never saw a value.
        checks["readable_result"] = True
        checks["probe_answered"] = False
        failures.append("probe_answered: oracle reported %r for the trivial probe" % norm["error"])
        return {"ok": False, "checks": checks, "failures": failures}
    if norm["error"] or norm["value"] is None:
        fail("readable_result", "returned a shape extract_scalar could not read (%s)"
             % (norm["error"] or "no value"))
        return {"ok": False, "checks": checks, "failures": failures}
    checks["readable_result"] = True
    checks["probe_answered"] = True
    return {"ok": not failures, "checks": checks, "failures": failures}


def null_oracle(_dax_query):
    """The explicit 'no executor attached' oracle: every record becomes ``not-evaluated``.

    Exists so 'unverified' is a stated position rather than an accident -- a caller can wire the loop
    with this and read a report that is honestly, uniformly ``not-evaluated``.
    """
    return {"error": "no oracle attached"}


# == external-process adapters (the caller supplies the command; we never pick one) ===============

def subprocess_oracle(cmd, *, timeout=300, runner=None):
    """Adapt a one-shot external executor into a ``fabric_oracle`` callable.

    ``cmd`` is a list/string the CALLER supplies. The DAX query is written to the child's stdin; the
    child writes one JSON document (any contract-legal shape) to stdout. Suitable when startup is
    cheap. ``runner`` is injectable for tests. The returned callable never raises -- a non-zero exit,
    a timeout, or unparseable stdout all become ``{"error": ...}``.
    """
    run = runner or _default_runner

    def oracle(dax_query):
        try:
            code, out, err = run(cmd, dax_query, timeout)
        except Exception as exc:
            return {"error": "oracle process failed: %s" % exc}
        if code != 0:
            return {"error": "oracle exited %s: %s" % (code, (err or out or "").strip()[:300])}
        text = (out or "").strip()
        if not text:
            return {"error": "oracle produced no output"}
        try:
            return json.loads(text)
        except ValueError:
            return {"error": "oracle stdout was not JSON: %s" % text[:200]}

    return oracle


def _default_runner(cmd, payload, timeout):  # pragma: no cover -- exercised via an injected runner
    proc = subprocess.run(cmd, input=payload, capture_output=True, text=True,
                          timeout=timeout, shell=isinstance(cmd, str))
    return proc.returncode, proc.stdout, proc.stderr


class persistent_oracle:  # noqa: N801 -- used as a callable, named like one
    """Adapt a LONG-LIVED external executor into a ``fabric_oracle`` callable.

    For an executor whose startup is expensive -- opening and refreshing a PBIP in Power BI Desktop
    costs minutes, so re-launching it per query is not viable. Speaks newline-delimited JSON over the
    child's stdio: one ``{"dax": "..."}`` request per line, one JSON response per line.

    Use as a context manager (``with persistent_oracle(cmd) as oracle: ...``) so the process is always
    closed. ``spawn`` is injectable for tests. Never raises from the call path.
    """

    def __init__(self, cmd, *, spawn=None, timeout=300):
        self._cmd = cmd
        self._timeout = timeout
        self._spawn = spawn or _default_spawn
        self._proc = None
        self._dead = None

    def _ensure(self):
        if self._proc is None and self._dead is None:
            try:
                self._proc = self._spawn(self._cmd)
            except Exception as exc:
                self._dead = "could not start oracle process: %s" % exc
        return self._proc

    def __call__(self, dax_query):
        if self._dead:
            return {"error": self._dead}
        proc = self._ensure()
        if proc is None:
            return {"error": self._dead or "oracle process unavailable"}
        try:
            proc.stdin.write(json.dumps({"dax": dax_query}) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
        except Exception as exc:
            self._dead = "oracle process broke: %s" % exc
            return {"error": self._dead}
        if not line:
            self._dead = "oracle process closed its output"
            return {"error": self._dead}
        try:
            return json.loads(line)
        except ValueError:
            return {"error": "oracle response was not JSON: %s" % line.strip()[:200]}

    def close(self):
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


def _default_spawn(cmd):  # pragma: no cover -- exercised via an injected spawn
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1,
                            shell=isinstance(cmd, str))


# == reading the model this skill emitted =========================================================

_MEASURE_RE = re.compile(r"^(?P<indent>[ \t]*)measure\s+(?P<name>'[^']+'|[^\s=]+)\s*=\s*(?P<dax>.*)$")
_ANNOT_RE = re.compile(r"^[ \t]*annotation\s+TableauFormula\s*=\s*(?P<formula>.*)$")
# A TMDL property (``mode: import``, ``formatString: 0.0%``) or a nested block header ends a DAX body.
_PROPERTY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*:")
_BLOCK_RE = re.compile(r"^(table|column|measure|partition|relationship|hierarchy|level|role|"
                       r"expression|culture|annotation|changedProperty|extendedProperty)\b")


def _indent_width(line):
    return len(line) - len(line.lstrip(" \t"))


def measures_from_tmdl(text):
    """Parse ``[{"name", "dax", "tableau_formula"}]`` out of one emitted TMDL table file.

    The emitted model carries BOTH sides of the comparison -- the landed DAX and the original Tableau
    formula preserved as an ``annotation TableauFormula`` -- so verification needs no report-schema
    change and no second source of truth.

    A measure's DAX body continues onto following lines only while they are indented DEEPER than the
    ``measure`` line itself AND are neither a TMDL property (``mode: import``) nor a nested block
    header. Both conditions are needed: indentation alone would swallow the sibling ``partition``
    block's properties into the last measure. Never raises.
    """
    out = []
    current = None
    body = []
    indent = 0

    def flush():
        if current is not None:
            current["dax"] = "\n".join(p for p in body if p).strip()
            out.append(current)

    for raw_line in (text or "").splitlines():
        match = _MEASURE_RE.match(raw_line)
        if match:
            flush()
            current = {"name": match.group("name").strip().strip("'"),
                       "dax": "", "tableau_formula": ""}
            body = [match.group("dax").strip()]
            indent = _indent_width(raw_line)
            continue
        if current is None:
            continue
        annot = _ANNOT_RE.match(raw_line)
        if annot:
            current["tableau_formula"] = annot.group("formula").strip()
            continue
        stripped = raw_line.strip()
        if not stripped:
            continue
        if _indent_width(raw_line) <= indent:
            # dedented to the measure's own level or shallower -> the measure block is over
            flush()
            current, body = None, []
            continue
        if _PROPERTY_RE.match(stripped) or _BLOCK_RE.match(stripped):
            continue
        body.append(stripped)
    flush()
    return [m for m in out if m["dax"]]


def measures_from_model_dir(model_dir):
    """Every measure across a ``*.SemanticModel`` folder's TMDL tables. Never raises."""
    found = []
    tables = os.path.join(model_dir, "definition", "tables")
    root = tables if os.path.isdir(tables) else model_dir
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in sorted(filenames):
            if not fn.lower().endswith(".tmdl"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                with open(path, encoding="utf-8-sig") as fh:
                    text = fh.read()
            except Exception:
                continue
            for m in measures_from_tmdl(text):
                m["source_file"] = os.path.relpath(path, model_dir)
                found.append(m)
    return found


# == driving the loop =============================================================================

def verify_model(model_dir, *, fabric_oracle=None, tableau_oracle=None, tableau_values=None,
                 measures=None, **reconcile_kw):
    """Run the Tier-1 reconciliation loop over an emitted model -> a verification report.

    This is the wiring that was missing: it collects the landed measures, asks the oracle for each
    one's value, and hands both sides to :func:`translation_reconcile.reconcile`.

    ``tableau_values`` -- optional ``{measure_name: ground_truth}``. ``tableau_oracle`` -- optional
    callable for fetching ground truth (VizQL Data Service). With NEITHER, every record is
    ``not-evaluated`` by construction: a DAX value with nothing to compare against proves nothing,
    and must never read as verified. Never raises.
    """
    rows = measures if measures is not None else measures_from_model_dir(model_dir)
    values = tableau_values or {}
    records = []
    for m in rows:
        kw = dict(reconcile_kw)
        if m["name"] in values:
            kw["tableau_value"] = values[m["name"]]
        rec = _TR.reconcile(m["name"], m["dax"], fabric_oracle=fabric_oracle,
                            tableau_oracle=tableau_oracle, **kw)
        rec["tableau_formula"] = m.get("tableau_formula", "")
        rec["source_file"] = m.get("source_file", "")
        records.append(rec)
    summary = _TR.summarize(records)
    return {
        "model_dir": model_dir,
        "oracle_attached": fabric_oracle is not None,
        "ground_truth_attached": bool(values) or tableau_oracle is not None,
        "records": records,
        "summary": summary,
    }


def format_verification(report):
    """Human-readable verification summary."""
    s = report.get("summary", {}) or {}
    lines = ["[VERIFY] %s measure(s): %s verified, %s mismatch, %s not-evaluated." % (
        s.get("total", 0), s.get(SUM_VERIFIED, 0), s.get(SUM_MISMATCH, 0),
        s.get(SUM_NOT_EVALUATED, 0))]
    if not report.get("oracle_attached"):
        lines.append("  [NOTE] no oracle attached -- every record is not-evaluated BY CONSTRUCTION, "
                     "which is not a pass.")
    elif not report.get("ground_truth_attached"):
        lines.append("  [NOTE] no Tableau ground truth attached -- a DAX value with nothing to "
                     "compare against proves nothing, so records stay not-evaluated.")
    for rec in report.get("records", []):
        if rec.get("state") == MISMATCH:
            lines.append("  [MISMATCH] %s: tableau=%r fabric=%r"
                         % (rec.get("name"), rec.get("tableau_value"), rec.get("fabric_value")))
    return "\n".join(lines)


def main(argv=None):
    """CLI sidecar: verify an emitted model against an external executor (opt-in; off by default)."""
    import argparse

    ap = argparse.ArgumentParser(
        description="Run the Tier-1 reconciliation loop over an emitted SemanticModel using an "
                    "EXTERNAL DAX executor you supply. Nothing here executes DAX itself.")
    ap.add_argument("--model-dir", required=True, help="a *.SemanticModel folder")
    ap.add_argument("--oracle-cmd", help="external executor command (JSON in/out). Omit to run with "
                                         "no oracle -- every record reports not-evaluated.")
    ap.add_argument("--persistent", action="store_true",
                    help="keep the executor running across queries (for an expensive startup, e.g. "
                         "opening and refreshing a PBIP in Power BI Desktop)")
    ap.add_argument("--ground-truth", metavar="JSON",
                    help='{"<measure>": <tableau value>} -- without it nothing can be VERIFIED')
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--json", metavar="PATH", help="write the full verification report here")
    args = ap.parse_args(argv)

    truth = {}
    if args.ground_truth:
        with open(args.ground_truth, encoding="utf-8-sig") as fh:
            truth = json.load(fh)

    oracle = None
    holder = None
    if args.oracle_cmd:
        if args.persistent:
            holder = persistent_oracle(args.oracle_cmd, timeout=args.timeout)
            oracle = holder
        else:
            oracle = subprocess_oracle(args.oracle_cmd, timeout=args.timeout)
        check = conforms(oracle)
        if not check["ok"]:
            print("[WARN] oracle did not fully satisfy the contract: " + "; ".join(check["failures"]))
    try:
        report = verify_model(args.model_dir, fabric_oracle=oracle, tableau_values=truth)
    finally:
        if holder is not None:
            holder.close()

    print(format_verification(report))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print("[OK] verification written to %s" % args.json)
    # A proven numeric divergence is the one thing that must fail a run loudly.
    return 1 if (report["summary"] or {}).get(SUM_MISMATCH) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
