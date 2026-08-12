"""Lead discovery from *permitted, local* sources.

Deliberately not a web scraper. Discovery reads:

  1. a candidate file (CSV or JSON) inside the worker's filesystem boundary, or
  2. the pre-existing DailySalesOS ``prospects`` table.

Live sourcing over the network would need an explicit ``egress_allow`` entry and a
connector decision from the operator; until that decision is made, pretending to
"discover from the web" would be the kind of fabrication this platform exists to
prevent. See "Limitations" in ``docs/SALES_INTEGRATION.md``.

Every candidate keeps its provenance: the file (with sha256) or the ledger row it
came from becomes a ``provenance`` evidence claim on the lead.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
from typing import Any, Dict, List, Optional, Tuple

from .models import Company, Contact
from .repository import SalesRepository, normalise_company

# Column aliases accepted in a candidate file. Anything else is ignored rather
# than guessed at.
_FIELDS = {
    "name": ("name", "company", "company_name", "business", "business_name"),
    "domain": ("domain", "website", "url", "site"),
    "industry": ("industry", "sector", "vertical"),
    "geography": ("geography", "city", "location", "market", "region"),
    "team_size": ("team_size", "agents", "size", "headcount", "employees"),
    "description": ("description", "notes", "about"),
    "contact_name": ("contact", "contact_name", "owner", "broker", "principal"),
    "contact_email": ("email", "contact_email"),
    "contact_role": ("role", "title", "contact_role"),
    "contact_phone": ("phone", "contact_phone", "telephone"),
}


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _pick(row: Dict[str, Any], key: str) -> str:
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for alias in _FIELDS[key]:
        if alias in lowered and lowered[alias] not in (None, ""):
            return str(lowered[alias]).strip()
    return ""


def _int(value: str) -> int:
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def read_candidates(path: str) -> Tuple[List[Dict[str, Any]], str]:
    """Read a CSV or JSON candidate file. Returns (rows, source_ref)."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"no candidate source at {path}")
    digest = file_sha256(path)
    source_ref = f"{path}#sha256:{digest[:12]}"
    if path.lower().endswith(".json"):
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        rows = data if isinstance(data, list) else data.get("companies") or data.get("rows") or []
    else:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
    if not isinstance(rows, list):
        raise ValueError(f"{path}: expected a list of candidate records")
    return [r for r in rows if isinstance(r, dict)], source_ref


def candidates_from_prospects(repo: SalesRepository, limit: int = 50) -> Tuple[List[Dict[str, Any]], str]:
    """Candidates from the pre-existing DailySalesOS ``prospects`` table."""
    rows = repo.raw(
        "SELECT id, company_name, contact_name, email, industry, role, team_size, "
        "geography, source FROM prospects ORDER BY created_at DESC LIMIT ?",
        (int(limit),),
    )
    out = []
    for r in rows:
        out.append(
            {
                "name": r.get("company_name") or "",
                "industry": r.get("industry") or "",
                "geography": r.get("geography") or "",
                "team_size": r.get("team_size") or 0,
                "contact_name": r.get("contact_name") or "",
                "contact_email": r.get("email") or "",
                "contact_role": r.get("role") or "",
                "source": r.get("source") or "prospects",
                "prospect_id": r.get("id") or "",
            }
        )
    return out, "experiments.db#table:prospects"


def discover(
    repo: SalesRepository,
    candidates: List[Dict[str, Any]],
    *,
    source_ref: str,
    source: str = "",
    limit: int = 0,
    run_id: str = "",
    evidence: Any = None,
) -> Dict[str, Any]:
    """Normalise, dedupe and create leads. Records provenance evidence.

    ``evidence`` is an optional ``SalesEvidence`` — when supplied, each new lead
    gets a ``provenance`` claim naming the exact source row, so ``why is this
    lead here`` is answerable from stored data.
    """
    created: List[Dict[str, Any]] = []
    duplicates: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    seen_keys: set = set()

    rows = candidates[: int(limit)] if limit else candidates
    for row in rows:
        name = _pick(row, "name") or str(row.get("name") or "").strip()
        domain = _pick(row, "domain")
        if not name and not domain:
            rejected.append({"row": row, "reason": "no company name or domain"})
            continue
        try:
            key = normalise_company(name, domain)
        except Exception as exc:
            rejected.append({"row": row, "reason": str(exc)})
            continue
        if key in seen_keys:
            duplicates.append({"name": name, "dedupe_key": key, "reason": "duplicate within this batch"})
            continue
        seen_keys.add(key)

        company = Company(
            name=name or domain,
            domain=domain,
            industry=_pick(row, "industry"),
            geography=_pick(row, "geography"),
            team_size=_int(_pick(row, "team_size")),
            description=_pick(row, "description"),
            website=domain,
            source=source or str(row.get("source") or ""),
        )
        result = repo.create_lead(
            company,
            source=company.source,
            prospect_id=str(row.get("prospect_id") or ""),
        )
        lead = result["lead"]
        if not result["created"]:
            duplicates.append(
                {"name": name, "dedupe_key": result["dedupe_key"], "lead_id": lead.id,
                 "reason": "already in the ledger"}
            )
            continue

        contact_name = _pick(row, "contact_name")
        if contact_name:
            repo.create_contact(
                Contact(
                    company_id=lead.company_id,
                    name=contact_name,
                    role=_pick(row, "contact_role"),
                    email=_pick(row, "contact_email"),
                    phone=_pick(row, "contact_phone"),
                    is_decision_maker=bool(
                        _pick(row, "contact_role")
                        and any(
                            w in _pick(row, "contact_role").lower()
                            for w in ("owner", "broker", "principal", "founder", "ceo", "team lead")
                        )
                    ),
                    source=source_ref,
                )
            )
        if evidence is not None:
            evidence.attach(
                lead.id,
                "provenance",
                f"Lead discovered from {source or 'candidate source'}: {name}",
                source_ref,
                tier="OBSERVED",
                excerpt=json.dumps(row, sort_keys=True, default=str)[:400],
                run_id=run_id,
            )
            if _pick(row, "contact_email"):
                evidence.attach(
                    lead.id,
                    "contact_info",
                    f"Public contact for {name}: {_pick(row, 'contact_email')}",
                    source_ref,
                    tier="OBSERVED",
                    run_id=run_id,
                )
        created.append(
            {
                "lead_id": lead.id,
                "company": company.name,
                "dedupe_key": result["dedupe_key"],
                "industry": company.industry,
            }
        )

    return {
        "source_ref": source_ref,
        "examined": len(rows),
        "created": created,
        "created_count": len(created),
        "duplicates": duplicates,
        "duplicate_count": len(duplicates),
        "rejected": rejected,
        "rejected_count": len(rejected),
    }
