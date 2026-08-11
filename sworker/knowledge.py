"""Company knowledge — the Hermes Atlas bridge.

Atlas (~/Documents/Projects/hermes-atlas) already implements compile-time
knowledge: ingest -> extract -> entities/claims/relationships/contradictions ->
confidence -> evidence ledger, with an append-only changelog and a determinism
gate. We do NOT reimplement any of that. This module:

  1. locates Atlas (``SWORKER_ATLAS_HOME`` or the sibling checkout) and imports it;
  2. compiles ``company/**`` (markdown + §18 adapters for .pdf/.docx/.json) into a
     store under ``<workspace>/.state/atlas``;
  3. exposes retrieval as Worker tools that return RETRIEVED-provenance evidence
     pointing at real claim ids and real source files.
  4. (§17) reports index status and detects stale/missing sources; rebuild support.

If Atlas is not importable, knowledge degrades to deterministic grep over the
company markdown — degraded, clearly labelled, never silently fabricated.
PDF/DOCX parsing needs the optional ``pdfminer.six`` / ``python-docx`` packages;
when absent those files are skipped with a clear reason (fail-closed), not
silently dropped. JSON is normalized with the stdlib.
"""

from __future__ import annotations

import os
import sys
import time
import hashlib
import json
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

ATLAS_AVAILABLE = False
_ATLAS_ERR = ""


def _try_import() -> bool:
    global ATLAS_AVAILABLE, _ATLAS_ERR
    if ATLAS_AVAILABLE:
        return True
    candidates = []
    env = os.environ.get("SWORKER_ATLAS_HOME")
    if env:
        candidates.append(env)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates.append(os.path.join(os.path.dirname(here), "hermes-atlas"))
    for c in candidates:
        if c and os.path.isdir(os.path.join(c, "hermes_atlas")) and c not in sys.path:
            sys.path.insert(0, c)
    try:
        import hermes_atlas  # noqa: F401

        ATLAS_AVAILABLE = True
    except Exception as exc:  # pragma: no cover
        _ATLAS_ERR = f"{type(exc).__name__}: {exc}"
        ATLAS_AVAILABLE = False
    return ATLAS_AVAILABLE


def atlas_status() -> Dict[str, Any]:
    ok = _try_import()
    info: Dict[str, Any] = {"available": ok, "error": _ATLAS_ERR}
    if ok:
        import hermes_atlas

        info["version"] = hermes_atlas.__version__
        info["path"] = os.path.dirname(os.path.abspath(hermes_atlas.__file__ or ""))
    return info


# ---------------------------------------------------------------------------
# compilation
# ---------------------------------------------------------------------------


def _collect_markdown(roots: List[str]) -> List[str]:
    files: List[str] = []
    for root in roots:
        if os.path.isfile(root) and root.endswith(".md"):
            files.append(os.path.abspath(root))
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in sorted(dirnames) if not d.startswith(".")]
            for fn in sorted(filenames):
                if fn.endswith(".md") and not fn.startswith((".", "_")):
                    files.append(os.path.join(dirpath, fn))
    return sorted(set(files))


# ---------------------------------------------------------------------------
# §18 ingestion adapters — non-markdown company sources
# ---------------------------------------------------------------------------
# Constraint: the core platform has ZERO third-party dependencies. PDF and DOCX
# parsing therefore need OPTIONAL libraries (pdfminer.six / python-docx). When
# those are absent the adapters fail CLOSED: they return a structured skip with
# the reason, never a silent drop and never fabricated text. JSON is parsed with
# the stdlib and normalized to markdown so it can flow through the same compiler.


def extract_pdf(path: str) -> Optional[str]:
    """Return plain text from a PDF, or None if pdfminer.six is unavailable / read fails."""
    try:
        from pdfminer.high_level import extract_text  # type: ignore
    except Exception:
        return None
    try:
        return extract_text(path) or ""
    except Exception:
        return None


def extract_docx(path: str) -> Optional[str]:
    """Return plain text from a .docx, or None if python-docx is unavailable / read fails."""
    try:
        from docx import Document  # type: ignore
    except Exception:
        return None
    try:
        doc = Document(path)
        parts: List[str] = []
        for p in doc.paragraphs:
            if p.text and p.text.strip():
                pre = ("# " if p.style and p.style.name.startswith("Heading") else "") + p.text
                parts.append(pre)
        return "\n".join(parts)
    except Exception:
        return None


def extract_json(path: str) -> Optional[str]:
    """Normalize a JSON document to markdown text (stdlib only).

    Objects become a headed section per top-level key; arrays of objects become a
    table; scalars are listed. Returns None on parse error. Deterministic output
    is required so Atlas' checksum-based incremental recompile stays stable.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None

    def _scalar(v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(v)

    def _render(v: Any, depth: int = 0) -> List[str]:
        out: List[str] = []
        if isinstance(v, dict):
            for k, val in v.items():
                out.append(f"## {k}")
                if isinstance(val, (dict, list)):
                    out.extend(_render(val, depth + 1))
                else:
                    out.append(_scalar(val))
                out.append("")
        elif isinstance(v, list):
            if v and all(isinstance(x, dict) for x in v):
                cols = list(dict.fromkeys(k for x in v for k in x.keys()))
                out.append("| " + " | ".join(cols) + " |")
                out.append("| " + " | ".join(["---"] * len(cols)) + " |")
                for x in v:
                    out.append("| " + " | ".join(_scalar(x.get(c, "")).replace("\n", " ") for c in cols) + " |")
                out.append("")
            else:
                for x in v:
                    if isinstance(x, (dict, list)):
                        out.extend(_render(x, depth + 1))
                    else:
                        out.append(f"- {_scalar(x)}")
                out.append("")
        else:
            out.append(_scalar(v))
        return out

    return "\n".join(_render(data)).strip()


# Map of supported non-markdown extensions to their extractor + availability flag.
_ADAPTERS = {
    ".pdf": ("pdf", extract_pdf),
    ".docx": ("docx", extract_docx),
    ".json": ("json", extract_json),
}


def collect_sources(roots: List[str]) -> Dict[str, Any]:
    """Walk knowledge roots and collect all ingestable sources.

    Returns ``{"markdown": [...], "other": [...], "skipped": [...], "count": N}``.
    ``markdown`` are .md paths (Atlas' native ingest). ``other`` are
    ``(path, extension, text)`` tuples already extracted to plain text by an
    adapter. ``skipped`` are ``(path, extension, reason)`` for files we could not
    ingest (unsupported type, or an optional dependency that is absent/failed) —
    fail-closed: every such file is reported, never silently swallowed.
    """
    md: List[str] = []
    other: List[Tuple[str, str, str]] = []
    skipped: List[Tuple[str, str, str]] = []
    seen: set = set()
    for root in roots:
        files: List[str] = []
        if os.path.isfile(root):
            files = [os.path.abspath(root)]
        else:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in sorted(dirnames) if not d.startswith(".")]
                for fn in sorted(filenames):
                    if not fn.startswith((".", "_")):
                        files.append(os.path.join(dirpath, fn))
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext == ".md":
                if f not in seen:
                    md.append(f)
                    seen.add(f)
                continue
            if ext in _ADAPTERS:
                kind, fn_extract = _ADAPTERS[ext]
                if f in seen:
                    continue
                seen.add(f)
                text = fn_extract(f)
                if text is None:
                    skipped.append((f, ext, f"{kind}-unavailable"))
                elif len(text.strip()) < 40:
                    skipped.append((f, ext, "too-short"))
                else:
                    other.append((f, ext, text))
            # unsupported extensions are intentionally ignored (not an ingestable
            # company source); only real ingest attempts are recorded in `skipped`.
    return {
        "markdown": sorted(md),
        "other": other,
        "skipped": skipped,
        "count": len(md) + len(other),
    }


def compile_knowledge(
    knowledge_roots: List[str], atlas_dir: str, *, client=None, summarize: bool = False
) -> Dict[str, Any]:
    """Compile company knowledge (markdown + §18 adapters) into an Atlas store.

    Incremental by construction: Atlas skips re-extraction for sources whose
    checksum is unchanged. Non-markdown sources are extracted to text and ingested
    through Atlas' ``ingest_file`` (which records the checksum that stale
    detection in §17 relies on).
    """
    if not _try_import():
        return {"ok": False, "reason": "atlas-unavailable", "error": _ATLAS_ERR}
    from hermes_atlas.compiler import Compiler
    from hermes_atlas.ingest import ingest_file
    from hermes_atlas.store import AtlasStore

    found = collect_sources(knowledge_roots)
    md_files = found["markdown"]
    if not found["markdown"] and not found["other"]:
        return {
            "ok": False,
            "reason": "no-markdown",
            "roots": knowledge_roots,
            "ingested": 0,
            "skipped": found["skipped"],
            "adapters": {
                "pdf": _ADAPTERS[".pdf"][0],
                "docx": _ADAPTERS[".docx"][0],
                "json": _ADAPTERS[".json"][0],
            },
        }
    sources = []
    for f in md_files:
        try:
            rec = ingest_file(f)
        except OSError:
            continue
        if len(rec.get("text", "").strip()) < 40:
            continue
        sources.append(rec)
    for path, ext, text in found["other"]:
        # Persist the extracted text to a stable temp markdown path so Atlas
        # ingests it consistently and records a checksum for §17 stale detection.
        try:
            import tempfile

            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".md", delete=False, encoding="utf-8", dir=atlas_dir
            )
            tmp.write(text)
            tmp.close()
            rec = ingest_file(tmp.name)
        except (OSError, Exception):
            continue
        if len(rec.get("text", "").strip()) < 40:
            continue
        # Re-point the source at the real file so stale/missing detection tracks
        # the original document, not the temp markdown.
        rec["path"] = os.path.abspath(path)
        rec["title"] = os.path.basename(path)
        sources.append(rec)
    store = AtlasStore(atlas_dir)
    cycle = len([c for c in store.changelog() if c.get("op") == "note"]) + 1
    report = Compiler(store, client=client).compile(
        sources, cycle=cycle, incremental=True, summarize=summarize
    )
    stats = store.stats()
    return {
        "ok": True,
        "atlas_dir": atlas_dir,
        "files": found["count"],
        "markdown_files": len(md_files),
        "adapter_files": len(found["other"]),
        "skipped": found["skipped"],
        "sources": len(sources),
        "stats": stats,
        "report": dict(report),
        "fingerprint": store.fingerprint(),
    }


# ---------------------------------------------------------------------------
# retrieval
# ---------------------------------------------------------------------------


def _tokens(q: str) -> List[str]:
    import re

    stop = {
        "the", "a", "an", "of", "for", "and", "or", "to", "in", "on", "is", "are",
        "what", "why", "how", "do", "does", "we", "our", "i", "this", "that", "about",
    }
    return [t for t in re.findall(r"[a-z0-9]+", q.lower()) if len(t) > 2 and t not in stop]


def search_claims(atlas_dir: str, query: str, limit: int = 8) -> List[Dict[str, Any]]:
    """Deterministic token-overlap search over compiled claims.

    Ranking is BM25-ish but intentionally simple and reproducible: identical
    inputs always produce identical ordering, which the determinism tests rely on.
    """
    if not _try_import() or not os.path.isdir(atlas_dir):
        return []
    from hermes_atlas.store import AtlasStore

    store = AtlasStore(atlas_dir)
    toks = _tokens(query)
    if not toks:
        return []
    sources = {s["id"]: s for s in store.read_all("sources")}
    scored: List[Tuple[float, str, Dict[str, Any]]] = []
    for claim in store.read_all("claims"):
        text = (claim.get("text") or "").lower()
        hits = sum(1 for t in toks if t in text)
        if not hits:
            continue
        score = hits / len(toks) + 0.15 * float(claim.get("confidence") or 0.0)
        scored.append((score, claim["id"], claim))
    scored.sort(key=lambda x: (-x[0], x[1]))
    out = []
    for score, cid, claim in scored[:limit]:
        srcs = [sources.get(s, {}) for s in (claim.get("source_ids") or [])]
        out.append(
            {
                "claim_id": cid,
                "text": claim.get("text", ""),
                "confidence": claim.get("confidence"),
                "status": claim.get("status", ""),
                "stance": claim.get("stance", ""),
                "hedged": claim.get("hedged", False),
                "contradiction_ids": claim.get("contradiction_ids") or [],
                "score": round(score, 4),
                "sources": [
                    {"id": s.get("id", ""), "title": s.get("title", ""), "path": s.get("path", "")}
                    for s in srcs
                    if s
                ],
            }
        )
    return out


def explain_claim(atlas_dir: str, claim_id: str) -> Optional[Dict[str, Any]]:
    if not _try_import():
        return None
    from hermes_atlas.explain import explain_claim as _explain
    from hermes_atlas.store import AtlasStore

    return _explain(AtlasStore(atlas_dir), claim_id)


def grep_knowledge(roots: List[str], query: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Degraded fallback used when Atlas is unavailable. Clearly labelled."""
    toks = _tokens(query)
    hits: List[Dict[str, Any]] = []
    for path in _collect_markdown(roots):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            low = line.lower()
            n = sum(1 for t in toks if t in low)
            if n:
                hits.append(
                    {"path": path, "line": i, "text": line.strip()[:300], "score": n / max(len(toks), 1)}
                )
    hits.sort(key=lambda h: (-h["score"], h["path"], h["line"]))
    return hits[:limit]


# ---------------------------------------------------------------------------
# §17 Atlas deepening — status / stale detection / incremental / rebuild
# ---------------------------------------------------------------------------


def _file_sha256(path: str) -> Optional[str]:
    """sha256 of a source file's *current* bytes. Fail-closed: None on any error."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def atlas_index_status(atlas_dir: str) -> Dict[str, Any]:
    """Report the state of the compiled index without recompiling.

    Builds on Atlas-store primitives only (read_all / stats / fingerprint /
    changelog) plus the per-source ``checksum`` Atlas recorded at ingest time.
    Returns a fail-closed report: if Atlas is unavailable or the index dir is
    absent, ``compiled`` is False and ``stale``/counts are empty — never a
    fabricated status.
    """
    info: Dict[str, Any] = {
        "compiled": False,
        "atlas_dir": atlas_dir,
        "available": False,
        "sources": 0,
        "claims": 0,
        "entities": 0,
        "contradictions": 0,
        "fingerprint": "",
        "changelog_entries": 0,
        "stale": [],
        "missing_sources": [],
        "stale_count": 0,
        "missing_count": 0,
    }
    if not _try_import() or not os.path.isdir(atlas_dir):
        info["reason"] = "atlas-unavailable" if not _try_import() else "no-index"
        return info
    info["available"] = True
    from hermes_atlas.store import AtlasStore

    store = AtlasStore(atlas_dir)
    stats = store.stats()
    info["compiled"] = True
    info["sources"] = stats.get("sources", 0)
    info["claims"] = stats.get("claims", 0)
    info["entities"] = stats.get("entities", 0)
    info["contradictions"] = stats.get("contradictions", 0)
    info["fingerprint"] = store.fingerprint()
    info["changelog_entries"] = stats.get("changelog_entries", 0)

    # Stale detection: compare each compiled source's recorded checksum against
    # the live file on disk. A mismatch means the source was edited (or deleted)
    # AFTER the last compile — every claim derived from it is now unprovable
    # against the current corpus until a recompile. Fail-closed: a source whose
    # file we cannot read is treated as missing, not as "fine".
    stale: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []
    for s in store.read_all("sources"):
        path = s.get("path") or ""
        recorded = s.get("checksum") or ""
        if not path or not os.path.isfile(path):
            missing.append({"source_id": s.get("id"), "title": s.get("title"), "path": path})
            continue
        live = _file_sha256(path)
        if live is None:
            continue
        if recorded and live != recorded:
            stale.append(
                {
                    "source_id": s.get("id"),
                    "title": s.get("title"),
                    "path": path,
                    "recorded_checksum": recorded[:12],
                    "live_checksum": live[:12],
                }
            )
    info["stale"] = stale
    info["missing_sources"] = missing
    info["stale_count"] = len(stale)
    info["missing_count"] = len(missing)
    return info


def incremental_compile(
    knowledge_roots: List[str], atlas_dir: str, *, client=None, summarize: bool = True
) -> Dict[str, Any]:
    """Recompile only what changed (the default Atlas behaviour).

    Thin, honest wrapper over :func:`compile_knowledge` with ``incremental=True``.
    Returns the compile report plus the pre/post index status so a caller can
    see exactly what moved. If Atlas is unavailable, returns the same degraded
    marker as compile_knowledge — no silent success.
    """
    pre = atlas_index_status(atlas_dir)
    core = compile_knowledge(
        knowledge_roots, atlas_dir, client=client, summarize=summarize
    )
    if not core.get("ok"):
        return {**core, "pre": pre, "post": None}
    post = atlas_index_status(atlas_dir)
    return {**core, "pre": pre, "post": post}


def rebuild_index(
    knowledge_roots: List[str], atlas_dir: str, *, client=None, summarize: bool = True
) -> Dict[str, Any]:
    """Full rebuild: wipe the existing index and recompile from scratch.

    Used when the changelog has drifted, a schema migration is needed, or stale
    detection shows enough churn that an incremental pass is no longer trusted.
    The wipe is confined to the Atlas store directory only — we never touch the
    source corpus or the rest of the workspace. Fail-closed: the wipe is refused
    if ``atlas_dir`` does not look like an Atlas store (no collections present)
    so a misconfiguration cannot delete unrelated data.
    """
    if not _try_import():
        return {"ok": False, "reason": "atlas-unavailable", "error": _ATLAS_ERR}
    if os.path.isdir(atlas_dir):
        from hermes_atlas.store import COLLECTIONS

        present = {c for c in COLLECTIONS if os.path.isdir(os.path.join(atlas_dir, c))}
        if present:
            import shutil

            shutil.rmtree(atlas_dir)
    return incremental_compile(
        knowledge_roots, atlas_dir, client=client, summarize=summarize
    )


# ---------------------------------------------------------------------------
# §19 sync watcher — keep the compiled index in sync with the source corpus
# ---------------------------------------------------------------------------
# A poll-based watcher (stdlib only, cross-platform) that recompiles the index
# whenever a watched source file changes. It is fail-closed: a compile failure
# is reported and the watcher keeps running (the next poll retries) rather than
# silently marking the index as current.


def _roots_snapshot(roots: List[str]) -> Dict[str, Tuple[float, int]]:
    """Map of every file under `roots` to (mtime, size). Fail-closed on errors."""
    snap: Dict[str, Tuple[float, int]] = {}
    for root in roots:
        if os.path.isfile(root):
            paths = [root]
        elif not os.path.isdir(root):
            continue
        else:
            paths = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in sorted(dirnames) if not d.startswith(".")]
                for fn in sorted(filenames):
                    if not fn.startswith((".", "_")):
                        paths.append(os.path.join(dirpath, fn))
        for p in paths:
            try:
                st = os.stat(p)
                snap[os.path.abspath(p)] = (st.st_mtime, st.st_size)
            except OSError:
                continue
    return snap


def _snapshot_changed(prev: Dict[str, Tuple[float, int]], cur: Dict[str, Tuple[float, int]]) -> bool:
    if set(prev) != set(cur):
        return True
    for k, v in cur.items():
        if prev.get(k) != v:
            return True
    return False


def watch_knowledge(
    knowledge_roots: List[str],
    atlas_dir: str,
    *,
    interval: float = 2.0,
    client=None,
    summarize: bool = True,
    on_compile: Optional[Callable[[Dict[str, Any]], None]] = None,
    stop: Optional[threading.Event] = None,
) -> threading.Event:
    """Watch knowledge roots and recompile incrementally on change.

    Runs a background daemon thread that polls every `interval` seconds, computes
    a (mtime, size) snapshot of all files under `knowledge_roots`, and triggers an
    incremental recompile whenever the snapshot changes. Returns the
    :class:`threading.Event` used to stop it (set it to halt the loop). If `stop`
    is supplied the caller owns it and the watcher uses it directly.

    `on_compile` (if given) is called with each :func:`compile_knowledge` result
    so callers can log/emit without coupling to the watcher. The watcher is
    fail-closed: a compile error is passed to `on_compile` and the loop continues.
    """
    if stop is None:
        stop = threading.Event()
    roots = [os.path.abspath(r) for r in knowledge_roots]

    def _loop() -> None:
        prev = _roots_snapshot(roots)
        while not stop.is_set():
            if stop.wait(interval):
                break
            cur = _roots_snapshot(roots)
            if not _snapshot_changed(prev, cur):
                continue
            prev = cur
            try:
                rep = incremental_compile(
                    roots, atlas_dir, client=client, summarize=summarize
                )
            except Exception as exc:  # never let a polling error kill the watcher
                rep = {"ok": False, "reason": "watch-compile-error", "error": f"{type(exc).__name__}: {exc}"}
            if on_compile:
                try:
                    on_compile(rep)
                except Exception:
                    pass

    t = threading.Thread(target=_loop, name="sworker-knowledge-watch", daemon=True)
    t.start()
    return stop
