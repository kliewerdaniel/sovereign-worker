"""Deterministic verification.

A Verification is a CHECK THAT RE-DERIVES A NUMBER FROM SOURCE DATA, written in
Python, with no model in the loop. If the recomputation disagrees with the
claim, the claim is marked REFUTED and the run degrades — it does not quietly
keep the nicer number.

Checks are declared as small dicts so procedures (YAML) can carry them:

    {"check": "recompute_sum", "path": "...", "value_column": "revenue",
     "where": {...}, "expect": 12345.6, "tolerance": 0.01}
"""

from __future__ import annotations

import math
import os
import re
from typing import Any, Callable, Dict, List, Optional

from dataclasses import dataclass, field

from .models import VerificationOutcome
from .tools.data import _matches, _num, file_sha256, read_csv


@dataclass
class CheckResult:
    """Pure result of a deterministic check.

    Kept separate from the persisted ``Verification`` record so checks stay pure
    functions the tests can call without a store or a run.
    """

    check: str
    status: VerificationOutcome
    detail: str = ""
    expected: Any = None
    actual: Any = None
    source_ref: str = ""

    @property
    def passed(self) -> bool:
        return self.status is VerificationOutcome.PASS


CheckFn = Callable[[Dict[str, Any], str], "CheckResult"]
_CHECKS: Dict[str, CheckFn] = {}


def check(name: str):
    def deco(fn: CheckFn) -> CheckFn:
        _CHECKS[name] = fn
        return fn

    return deco


def available_checks() -> List[str]:
    return sorted(_CHECKS)


def run_check(spec: Dict[str, Any], workspace: str) -> CheckResult:
    name = spec.get("check", "")
    fn = _CHECKS.get(name)
    if fn is None:
        return CheckResult(
            check=name or "(unnamed)",
            status=VerificationOutcome.UNVERIFIABLE,
            detail=f"unknown check {name!r}; available: {available_checks()}",
        )
    try:
        return fn(spec, workspace)
    except Exception as exc:
        return CheckResult(
            check=name,
            status=VerificationOutcome.UNVERIFIABLE,
            detail=f"{type(exc).__name__}: {exc}",
        )


def _resolve(workspace: str, path: str) -> str:
    p = os.path.realpath(os.path.join(workspace, os.path.expanduser(path)))
    root = os.path.realpath(workspace)
    if not (p == root or p.startswith(root + os.sep)):
        raise ValueError(f"verification path {path!r} escapes the workspace")
    return p


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


@check("recompute_sum")
def _recompute_sum(spec: Dict[str, Any], workspace: str) -> Verification:
    """Independently re-sum a CSV column and compare with the claimed total."""
    path = _resolve(workspace, spec["path"])
    rows = read_csv(path)
    where = spec.get("where") or {}
    col = spec["value_column"]
    vals = [
        n
        for n in (_num(r.get(col)) for r in rows if _matches(r, where))
        if n is not None
    ]
    actual = round(sum(vals), 6)
    expect = _num(spec.get("expect"))
    tol = float(spec.get("tolerance", 0.01))
    if expect is None:
        return CheckResult(
            check="recompute_sum",
            status=VerificationOutcome.UNVERIFIABLE,
            detail=f"recomputed {col} = {actual} over {len(vals)} rows, but no expected value given",
            expected=None,
            actual=actual,
            source_ref=f"{path}#sha256:{file_sha256(path)[:12]}",
        )
    ok = math.isclose(actual, expect, rel_tol=0, abs_tol=max(tol, abs(expect) * 0.001))
    return CheckResult(
        check="recompute_sum",
        status=VerificationOutcome.PASS if ok else VerificationOutcome.FAIL,
        detail=(
            f"independently summed {col} over {len(vals)} rows of "
            f"{os.path.basename(path)}: {actual} vs claimed {expect}"
            + ("" if ok else f" — MISMATCH (tolerance {tol})")
        ),
        expected=expect,
        actual=actual,
        source_ref=f"{path}#sha256:{file_sha256(path)[:12]}",
    )


@check("recompute_delta_pct")
def _recompute_delta(spec: Dict[str, Any], workspace: str) -> Verification:
    """Re-derive a percentage change between two filtered windows of one CSV."""
    path = _resolve(workspace, spec["path"])
    rows = read_csv(path)
    col = spec["value_column"]

    def total(where: Dict[str, Any]) -> float:
        vals = [
            n for n in (_num(r.get(col)) for r in rows if _matches(r, where)) if n is not None
        ]
        return round(sum(vals), 6)

    cur = total(spec.get("current") or {})
    prev = total(spec.get("previous") or {})
    if prev == 0:
        return CheckResult(
            check="recompute_delta_pct",
            status=VerificationOutcome.UNVERIFIABLE,
            detail=f"previous period total is 0; percentage change undefined (current={cur})",
            actual=cur,
            source_ref=path,
        )
    actual = round((cur - prev) / prev * 100.0, 4)
    expect = _num(spec.get("expect"))
    if expect is None:
        return CheckResult(
            check="recompute_delta_pct",
            status=VerificationOutcome.UNVERIFIABLE,
            detail=f"recomputed change = {actual}% (current {cur} vs previous {prev})",
            actual=actual,
            source_ref=path,
        )
    tol = float(spec.get("tolerance", 0.5))
    ok = abs(actual - expect) <= tol
    return CheckResult(
        check="recompute_delta_pct",
        status=VerificationOutcome.PASS if ok else VerificationOutcome.FAIL,
        detail=(
            f"recomputed {col} change: {actual}% (current {cur} vs previous {prev}); "
            f"claimed {expect}%" + ("" if ok else f" — MISMATCH (tolerance {tol}pp)")
        ),
        expected=expect,
        actual=actual,
        source_ref=f"{path}#sha256:{file_sha256(path)[:12]}",
    )


@check("row_count")
def _row_count(spec: Dict[str, Any], workspace: str) -> Verification:
    path = _resolve(workspace, spec["path"])
    rows = read_csv(path)
    matched = [r for r in rows if _matches(r, spec.get("where") or {})]
    actual = float(len(matched))
    expect = _num(spec.get("expect"))
    if expect is None:
        return CheckResult(
            check="row_count",
            status=VerificationOutcome.UNVERIFIABLE,
            detail=f"{len(matched)} rows matched",
            actual=actual,
            source_ref=path,
        )
    ok = actual == expect
    return CheckResult(
        check="row_count",
        status=VerificationOutcome.PASS if ok else VerificationOutcome.FAIL,
        detail=f"{len(matched)} rows matched, expected {int(expect)}",
        expected=expect,
        actual=actual,
        source_ref=path,
    )


@check("file_exists")
def _file_exists(spec: Dict[str, Any], workspace: str) -> Verification:
    path = _resolve(workspace, spec["path"])
    ok = os.path.isfile(path)
    size = os.path.getsize(path) if ok else 0
    min_bytes = int(spec.get("min_bytes", 1))
    passed = ok and size >= min_bytes
    return CheckResult(
        check="file_exists",
        status=VerificationOutcome.PASS if passed else VerificationOutcome.FAIL,
        detail=(
            f"{os.path.basename(path)} exists ({size} bytes)"
            if ok
            else f"{spec['path']} does not exist"
        )
        + ("" if passed else f" — required at least {min_bytes} bytes"),
        expected=float(min_bytes),
        actual=float(size),
        source_ref=path,
    )


@check("artifact_contains_evidence")
def _artifact_contains_evidence(spec: Dict[str, Any], workspace: str) -> Verification:
    """A report that states numbers must also cite where they came from."""
    path = _resolve(workspace, spec["path"])
    if not os.path.isfile(path):
        return CheckResult(
            check="artifact_contains_evidence",
            status=VerificationOutcome.FAIL,
            detail=f"artifact {spec['path']} does not exist",
            source_ref=path,
        )
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    markers = spec.get("markers") or ["Evidence:", "evidence"]
    found = sum(1 for m in markers if m.lower() in text.lower())
    minimum = int(spec.get("min_mentions", 1))
    ok = found >= minimum
    return CheckResult(
        check="artifact_contains_evidence",
        status=VerificationOutcome.PASS if ok else VerificationOutcome.FAIL,
        detail=(
            f"{os.path.basename(path)} references evidence "
            f"({found}/{len(markers)} markers found, need {minimum})"
        ),
        expected=float(minimum),
        actual=float(found),
        source_ref=path,
    )


@check("provenance_chain")
def _provenance_chain(spec: Dict[str, Any], workspace: str) -> Verification:
    """Close the provenance gap: a claimed figure must (a) re-derive from its
    cited source rows AND (b) be cited by the artifact that states it.

    This is the link the numeric-only checks miss: it proves the number in the
    report is the SAME number the source produces, not a copied-from-elsewhere
    or hand-typed figure. Both halves must hold or the chain is broken.
    """
    # part (a): re-derive from source
    path = _resolve(workspace, spec["path"])
    rows = read_csv(path)
    where = spec.get("where") or {}
    col = spec["value_column"]
    vals = [n for n in (_num(r.get(col)) for r in rows if _matches(r, where)) if n is not None]
    actual = round(sum(vals), 6)
    expect = _num(spec.get("expect"))
    if expect is None:
        return CheckResult(
            check="provenance_chain",
            status=VerificationOutcome.UNVERIFIABLE,
            detail=f"recomputed {col} = {actual}; no expected value to chain",
            expected=None, actual=actual,
            source_ref=f"{path}#sha256:{file_sha256(path)[:12]}",
        )
    derive_ok = math.isclose(actual, expect, rel_tol=0, abs_tol=max(0.01, abs(expect) * 0.001))
    derive_detail = (
        f"source re-sum of {col} = {actual} matches claimed {expect}"
        if derive_ok else
        f"source re-sum of {col} = {actual} MISMATCHES claimed {expect}"
    )
    # part (b): the artifact that states the figure must cite the source
    artifact = spec.get("artifact")
    if artifact:
        rp = _resolve(workspace, artifact)
        if not os.path.isfile(rp):
            cite_ok = False
            cite_detail = f"artifact {artifact} not found — cannot confirm citation"
        else:
            with open(rp, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            needles = [f"{actual:,.2f}", f"{actual:.2f}", f"{int(actual):,}"]
            cite_ok = any(n in text for n in needles)
            cite_detail = (
                f"artifact cites the derived figure"
                if cite_ok else
                f"artifact DOES NOT cite the derived figure {actual}"
            )
    else:
        cite_ok = True
        cite_detail = "no artifact claimed (source-only check)"
    ok = derive_ok and cite_ok
    return CheckResult(
        check="provenance_chain",
        status=VerificationOutcome.PASS if ok else VerificationOutcome.FAIL,
        detail=f"{derive_detail}; {cite_detail}",
        expected=expect, actual=actual,
        source_ref=f"{path}#sha256:{file_sha256(path)[:12]}",
    )


@check("totals_match_source")
def _totals_match_source(spec: Dict[str, Any], workspace: str) -> Verification:
    """Every number the report claims for `value_column` must equal the CSV sum."""
    src = _resolve(workspace, spec["path"])
    rows = read_csv(src)
    col = spec["value_column"]
    vals = [
        n
        for n in (_num(r.get(col)) for r in rows if _matches(r, spec.get("where") or {}))
        if n is not None
    ]
    actual = round(sum(vals), 2)
    report = _resolve(workspace, spec["artifact"])
    if not os.path.isfile(report):
        return CheckResult(
            check="totals_match_source",
            status=VerificationOutcome.FAIL,
            detail=f"artifact {spec['artifact']} not found; cannot compare totals",
            actual=actual,
            source_ref=src,
        )
    with open(report, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    needle = f"{actual:,.2f}"
    alt = f"{actual:.2f}"
    ok = needle in text or alt in text or f"{int(actual):,}" in text
    return CheckResult(
        check="totals_match_source",
        status=VerificationOutcome.PASS if ok else VerificationOutcome.FAIL,
        detail=(
            f"source total for {col} is {actual}; artifact "
            f"{'cites' if ok else 'DOES NOT cite'} that figure"
        ),
        expected=actual,
        actual=actual if ok else None,
        source_ref=f"{src}#sha256:{file_sha256(src)[:12]}",
    )


# ---------------------------------------------------------------------------
# §15 generalized verification framework
#
# The checks above re-derive NUMBERS from source. The framework below closes
# the other half of the gap: structural / non-numeric claims (format, presence
# of required records, set membership, exact document text) that a report or
# data file asserts but a model could just as easily have fabricated. Every
# check re-reads the SOURCE itself — no model in the loop.
# ---------------------------------------------------------------------------


@check("schema")
def _schema(spec: Dict[str, Any], workspace: str) -> Verification:
    """A CSV must carry the required columns, with optional types/non-nullness."""
    src = _resolve(workspace, spec["path"])
    if not os.path.isfile(src):
        return CheckResult(
            check="schema",
            status=VerificationOutcome.UNVERIFIABLE,
            detail=f"source {spec['path']} not found — cannot verify schema",
            source_ref=src,
        )
    rows = read_csv(src)
    if not rows:
        return CheckResult(
            check="schema",
            status=VerificationOutcome.FAIL,
            detail=f"{os.path.basename(src)} has no header row",
            source_ref=src,
        )
    cols = list(rows[0].keys())
    required = spec.get("required_columns") or []
    missing = [c for c in required if c not in cols]
    if missing:
        return CheckResult(
            check="schema",
            status=VerificationOutcome.FAIL,
            detail=f"{os.path.basename(src)} missing required columns {missing}; have {cols}",
            expected=sorted(required),
            actual=cols,
            source_ref=src,
        )
    # optional type / non-null assertions on required columns
    type_reqs = spec.get("column_types") or {}
    null_reqs = spec.get("non_null") or []
    bad = []
    for r in rows:
        for c in null_reqs:
            if c in cols and (r.get(c) is None or str(r.get(c)).strip() == ""):
                bad.append(f"{c} empty in row {rows.index(r) + 2}")
                break
        for c, t in type_reqs.items():
            if c not in cols:
                continue
            val = r.get(c)
            if val is None or str(val).strip() == "":
                continue
            if t == "number" and _num(val) is None:
                bad.append(f"{c}={val!r} not a number")
            elif t == "int":
                n = _num(val)
                if n is None:
                    bad.append(f"{c}={val!r} not an int")
                elif n != int(n):
                    bad.append(f"{c}={val!r} not an integer")
            elif t in ("bool",) and str(val).strip().lower() not in ("true", "false", "0", "1", "yes", "no"):
                bad.append(f"{c}={val!r} not a bool")
    if bad:
        return CheckResult(
            check="schema",
            status=VerificationOutcome.FAIL,
            detail=f"{os.path.basename(src)}: {len(bad)} row violation(s): {bad[:3]}",
            expected=sorted(required),
            actual=cols,
            source_ref=src,
        )
    return CheckResult(
        check="schema",
        status=VerificationOutcome.PASS,
        detail=f"{os.path.basename(src)} has required columns {sorted(required)} over {len(rows)} rows"
        + (f"; types {type_reqs}" if type_reqs else "")
        + (f"; non-null {null_reqs}" if null_reqs else ""),
        expected=sorted(required),
        actual=cols,
        source_ref=src,
    )


@check("set_equality")
def _set_equality(spec: Dict[str, Any], workspace: str) -> Verification:
    """The distinct values of a column must equal exactly a required set.

    Proves a categorical claim (e.g. "we operate in North/South/Online") is
    complete and exact — no extra, no missing regions.
    """
    src = _resolve(workspace, spec["path"])
    if not os.path.isfile(src):
        return CheckResult(
            check="set_equality",
            status=VerificationOutcome.UNVERIFIABLE,
            detail=f"source {spec['path']} not found",
            source_ref=src,
        )
    rows = read_csv(src)
    col = spec["value_column"]
    if rows and col not in rows[0]:
        return CheckResult(
            check="set_equality",
            status=VerificationOutcome.FAIL,
            detail=f"column {col!r} not present in {os.path.basename(src)}",
            source_ref=src,
        )
    actual = sorted({str(r.get(col, "")) for r in rows if str(r.get(col, "")).strip()})
    expected = sorted(str(x) for x in (spec.get("expected") or []))
    if not expected:
        return CheckResult(
            check="set_equality",
            status=VerificationOutcome.UNVERIFIABLE,
            detail=f"distinct {col} = {actual}; no expected set given",
            expected=None,
            actual=actual,
            source_ref=src,
        )
    ok = actual == expected
    return CheckResult(
        check="set_equality",
        status=VerificationOutcome.PASS if ok else VerificationOutcome.FAIL,
        detail=(
            f"distinct {col} = {actual}"
            + (f" matches expected {expected}" if ok else f" MISMATCHES expected {expected}")
        ),
        expected=expected,
        actual=actual,
        source_ref=src,
    )


@check("regex")
def _regex(spec: Dict[str, Any], workspace: str) -> Verification:
    """A required pattern must be present (or absent) in the source text/file.

    Used to prove a stated identifier/format (order id, SKU, date, config
    token) actually appears in the cited artifact — not just asserted in prose.
    """
    path = _resolve(workspace, spec["path"])
    if not os.path.isfile(path):
        return CheckResult(
            check="regex",
            status=VerificationOutcome.UNVERIFIABLE,
            detail=f"{spec['path']} not found",
            source_ref=path,
        )
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    pattern = spec["pattern"]
    try:
        rx = re.compile(pattern, re.MULTILINE)
    except re.error as exc:
        return CheckResult(
            check="regex",
            status=VerificationOutcome.UNVERIFIABLE,
            detail=f"invalid regex {pattern!r}: {exc}",
            source_ref=path,
        )
    present = bool(rx.search(text))
    want_present = bool(spec.get("present", True))
    ok = present == want_present
    matched = rx.findall(text)
    return CheckResult(
        check="regex",
        status=VerificationOutcome.PASS if ok else VerificationOutcome.FAIL,
        detail=(
            f"regex {pattern!r} {'found' if present else 'NOT found'} in "
            f"{os.path.basename(path)}"
            + (f" ({len(matched)} match(es))" if present else "")
            + (f"; expected {'presence' if want_present else 'absence'}"
               if ok else f" — MISMATCH (wanted {'presence' if want_present else 'absence'})")
        ),
        expected=pattern,
        actual=bool(present),
        source_ref=path,
    )


@check("doc_section")
def _doc_section(spec: Dict[str, Any], workspace: str) -> Verification:
    """An exact or substring claim about a headed document section must hold.

    Proves the report/README actually contains the stated prose (e.g. a
    methodology section, a legal disclaimer, an approval note) — re-derived
    from the document itself, side-stepping a model paraphrasing it.
    """
    path = _resolve(workspace, spec["path"])
    if not os.path.isfile(path):
        return CheckResult(
            check="doc_section",
            status=VerificationOutcome.UNVERIFIABLE,
            detail=f"{spec['path']} not found",
            source_ref=path,
        )
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    heading = spec.get("heading")
    needle = spec.get("contains")
    if heading:
        # extract the section body: from the heading line until the next line
        # that looks like a heading (same leading # count) or EOF.
        lines = text.splitlines()
        body = []
        in_section = False
        hlevel = len(heading) - len(heading.lstrip("#"))
        for ln in lines:
            if in_section:
                if ln.startswith("#") and len(ln) - len(ln.lstrip("#")) <= hlevel and ln.strip() != heading.strip():
                    break
                body.append(ln)
            elif ln.strip() == heading.strip():
                in_section = True
        section = "\n".join(body).strip()
        if not in_section:
            return CheckResult(
                check="doc_section",
                status=VerificationOutcome.FAIL,
                detail=f"heading {heading!r} not found in {os.path.basename(path)}",
                source_ref=path,
            )
        if needle is None:
            return CheckResult(
                check="doc_section",
                status=VerificationOutcome.PASS,
                detail=f"section {heading!r} present ({len(section)} chars)",
                source_ref=path,
            )
        ok = needle in section
        return CheckResult(
            check="doc_section",
            status=VerificationOutcome.PASS if ok else VerificationOutcome.FAIL,
            detail=(
                f"section {heading!r} {'contains' if ok else 'DOES NOT contain'} {needle!r}"
            ),
            expected=needle,
            actual=bool(ok),
            source_ref=path,
        )
    # no heading -> whole-document substring check
    if needle is None:
        return CheckResult(
            check="doc_section",
            status=VerificationOutcome.UNVERIFIABLE,
            detail="doc_section needs 'heading' or 'contains'",
            source_ref=path,
        )
    ok = needle in text
    return CheckResult(
        check="doc_section",
        status=VerificationOutcome.PASS if ok else VerificationOutcome.FAIL,
        detail=f"{os.path.basename(path)} {'contains' if ok else 'DOES NOT contain'} {needle!r}",
        expected=needle,
        actual=bool(ok),
        source_ref=path,
    )
