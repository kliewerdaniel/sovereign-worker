"""Compile the DailySalesOS markdown into the sales ontology.

The markdown stays the human-readable source of truth. This module is the
*compiler*: it parses the documents that define offer, ICP, targets and follow-up
sequences, and records where each value came from (`source_doc` + line) so nothing
in the database is unattributable.

Retrieval over the same corpus reuses the existing Atlas bridge
(``sworker.knowledge`` / ``tools/knowledge.py``): compiled claims when Atlas is
installed, labelled grep when it is not. Nothing new is built here for search.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from .. import knowledge as K
from .models import ICP

# Documents this compiler reads, relative to the DailySalesOS root.
DOCS = {
    "icp": "Industry_Ranking.md",
    "offer": "Core_Offer.md",
    "metrics": "Metrics_Single_Source_of_Truth.md",
    "pipeline": "CRM_Pipeline.md",
    "rubric": "Discovery_Rubric.md",
    "followup": "Follow_Up_System.md",
    "claims": "Hypothesis_Log.md",
}

_ICP_HEADING = re.compile(r"^###\s+(\d+)\.\s+(.+?)\s+—\s+Score:\s*([0-9.]+)\s*/\s*5\s*$")
_MONEY = re.compile(r"\$([0-9][0-9,]*)")
_TEAM_SIZE = re.compile(r"(\d+)\s*\+?\s*agents", re.I)


def _read(root: str, name: str) -> Optional[Tuple[str, str]]:
    path = os.path.join(root, name)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read(), path


def docs_root(workspace: str = "") -> str:
    """Best-effort path to the DailySalesOS markdown docs (read-only source of truth).

    Consults ``DAILYSALESOS_ROOT`` first (set by the operator), then falls back to a
    ``sales_knowledge/`` dir inside the worker's workspace. The sales layer works
    on the ledger alone when neither is present — the parse_* functions simply
    return empty/``found=False`` and the run degrades rather than inventing values.
    """
    env = os.environ.get("DAILYSALESOS_ROOT", "")
    if env and os.path.isdir(env):
        return env
    if workspace:
        cand = os.path.join(workspace, "sales_knowledge")
        if os.path.isdir(cand):
            return cand
    return ""


def parse_industry_ranking(root: str) -> List[Dict[str, Any]]:
    """Ranked industries from ``Industry_Ranking.md``. Empty if absent."""
    got = _read(root, DOCS["icp"])
    if not got:
        return []
    text, path = got
    out: List[Dict[str, Any]] = []
    for i, line in enumerate(text.splitlines(), start=1):
        m = _ICP_HEADING.match(line.strip())
        if m:
            out.append(
                {
                    "rank": int(m.group(1)),
                    "name": m.group(2).strip(),
                    "rank_score": float(m.group(3)),
                    "source_doc": f"{os.path.basename(path)}:{i}",
                }
            )
    return out


def parse_core_offer(root: str) -> Dict[str, Any]:
    """Offer name, price and target-customer constraints from ``Core_Offer.md``."""
    got = _read(root, DOCS["offer"])
    if not got:
        return {}
    text, path = got
    lines = text.splitlines()
    offer_name = ""
    for i, line in enumerate(lines):
        if line.strip().startswith("## Offer Name"):
            for nxt in lines[i + 1 : i + 4]:
                cleaned = nxt.strip().strip("*")
                if cleaned:
                    offer_name = cleaned
                    break
            break
    price = 0.0
    for line in lines:
        if "**Fee:**" in line and "flat" in line.lower():
            m = _MONEY.search(line)
            if m:
                price = float(m.group(1).replace(",", ""))
                break
    if not price:
        m = _MONEY.search(text)
        if m:
            price = float(m.group(1).replace(",", ""))
    team_size = 0
    for line in lines:
        if "Target Customer" in line:
            idx = lines.index(line)
            blob = " ".join(lines[idx : idx + 4])
            tm = _TEAM_SIZE.search(blob)
            if tm:
                team_size = int(tm.group(1))
            break
    return {
        "offer": offer_name,
        "offer_price": price,
        "min_team_size": team_size,
        "source_doc": os.path.basename(path),
    }


# Daily targets in Metrics_Single_Source_of_Truth.md, matched by their own wording.
_TARGET_PATTERNS = [
    ("prospects_researched", re.compile(r"(\d+)\s+prospects\s+researched", re.I)),
    ("outreach_sent", re.compile(r"(\d+)\s+new\s+outreach\s+messages\s+sent", re.I)),
    ("followups_sent", re.compile(r"(\d+)\s+follow-ups\s+sent", re.I)),
    ("discoveries_completed", re.compile(r"(\d+)\s+discovery\s+calls?\s+completed", re.I)),
    ("discoveries_scheduled", re.compile(r"(\d+)\s+discovery\s+calls?\s+scheduled", re.I)),
]


def parse_daily_targets(root: str) -> Dict[str, Any]:
    """The non-negotiable daily minimums, read from the single source of truth.

    Returns ``{"targets": {...}, "source_doc": ..., "found": bool}``. Targets are
    never hard-coded elsewhere in the sales layer: the analyst reports against
    whatever this document says.
    """
    got = _read(root, DOCS["metrics"])
    if not got:
        return {"targets": {}, "source_doc": "", "found": False}
    text, path = got
    targets: Dict[str, int] = {}
    refs: Dict[str, str] = {}
    for i, line in enumerate(text.splitlines(), start=1):
        for key, pat in _TARGET_PATTERNS:
            if key in targets:
                continue
            m = pat.search(line)
            if m:
                targets[key] = int(m.group(1))
                refs[key] = f"{os.path.basename(path)}:{i}"
    return {
        "targets": targets,
        "refs": refs,
        "source_doc": os.path.basename(path),
        "path": path,
        "found": bool(targets),
    }


_SEQ_HEADING = re.compile(r"^###\s+(.+?)\s+Sequence\s*$")
_SEQ_STEP = re.compile(r"^(?:Same day|Day\s+(\d+)):\s*(.+)$", re.I)


def parse_followup_sequences(root: str) -> Dict[str, List[Dict[str, Any]]]:
    """Day-offset sequences from ``Follow_Up_System.md``.

    ``{"New Prospect": [{"day": 0, "action": "Initial outreach email"}, ...]}``
    """
    got = _read(root, DOCS["followup"])
    if not got:
        return {}
    text, _ = got
    sequences: Dict[str, List[Dict[str, Any]]] = {}
    current: Optional[str] = None
    for line in text.splitlines():
        stripped = line.strip()
        m = _SEQ_HEADING.match(stripped)
        if m:
            current = m.group(1).strip()
            sequences[current] = []
            continue
        if current is not None:
            s = _SEQ_STEP.match(stripped)
            if s:
                day = int(s.group(1)) if s.group(1) is not None else 0
                sequences.setdefault(current, []).append(
                    {"day": day, "action": s.group(2).strip()}
                )
    return {k: v for k, v in sequences.items() if v}


def compile_icp(root: str) -> List[ICP]:
    """Build ICP records from Industry_Ranking.md + Core_Offer.md.

    Only the top-ranked industry is marked active by default: the offer document
    names one target customer, and activating every industry would make the ICP
    fit score meaningless.
    """
    offer = parse_core_offer(root)
    out: List[ICP] = []
    for entry in parse_industry_ranking(root):
        out.append(
            ICP(
                name=entry["name"],
                industry=entry["name"],
                rank=entry["rank"],
                rank_score=entry["rank_score"],
                min_team_size=offer.get("min_team_size", 0) if entry["rank"] == 1 else 0,
                offer=offer.get("offer", ""),
                offer_price=offer.get("offer_price", 0.0),
                source_doc=entry["source_doc"],
                active=entry["rank"] == 1,
            )
        )
    return out


def retrieve(workspace: str, query: str, limit: int = 8) -> Dict[str, Any]:
    """Search the sales knowledge corpus through the EXISTING Atlas bridge.

    Compiled claims when Atlas is available; labelled grep otherwise, with the
    degradation stated in the payload rather than hidden.
    """
    atlas_dir = os.path.join(workspace, ".state", "atlas")
    claims = K.search_claims(atlas_dir, query, limit=limit)
    if claims:
        return {"mode": "compiled", "degraded": False, "claims": claims}
    roots = [os.path.join(workspace, "company")]
    hits = K.grep_knowledge(roots, query, limit=limit)
    return {
        "mode": "grep" if hits else "empty",
        "degraded": True,
        "hits": hits,
        "note": "knowledge not compiled; labelled document grep only",
    }
