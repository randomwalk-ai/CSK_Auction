"""
Squad impact insight — who a pool player fills or upgrades vs current CSK squad.

Lightweight: CSK fit + gaps for squad; full valuation for candidate only.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable, Dict, List, Optional

from auction_constants import IPL_PURSE_CR
from player_loader import get_player_stats
from player_metadata import fetch_metadata as fetch_player_metadata
from valuation_engine import CSKFitEngine, FranchiseValuationEngine, RoleInferenceEngine
from war_room import IDEAL_ROLE, MAX_OVERSEAS, normalize_role, squad_gap_from_list

MetadataFn = Callable[[str, Optional[str]], Dict[str, Any]]

PROTECTED_FIT_THRESHOLD = 68


def _norm_name(name: str) -> str:
    return (name or "").strip().lower()


def _is_overseas(country: str) -> bool:
    c = (country or "").strip().lower()
    return c not in ("", "india", "indian", "ind")


def _is_protected_replace_target(row: Dict[str, Any], profile: Dict[str, Any]) -> bool:
    if row.get("retained") is True:
        return True
    if row.get("droppable") is False and row.get("price_verified"):
        return True
    fit = float(profile.get("csk_fit") or 0)
    if row.get("price_verified") and fit >= PROTECTED_FIT_THRESHOLD:
        return True
    return False


def _light_profile(
    conn: sqlite3.Connection,
    name: str,
    *,
    role_engine: RoleInferenceEngine,
    csk_engine: CSKFitEngine,
    metadata_fn: MetadataFn,
    price_cr: float = 0.0,
) -> Optional[Dict[str, Any]]:
    stats = get_player_stats(conn, name)
    if not stats:
        return None
    db_name = stats["player_name"]
    meta = metadata_fn(db_name, name)
    role, role_detail = role_engine.infer(stats)
    fit, reasons = csk_engine.score(stats, role, meta)
    return {
        "name": db_name,
        "display_name": name.strip(),
        "role": role,
        "role_detail": role_detail,
        "csk_fit": round(float(fit), 1),
        "form": round(float(stats.get("form_rating") or 50), 1),
        "reasons": list(reasons or []),
        "price_cr": round(float(price_cr or 0), 2),
        "matches_played": int(stats.get("matches_played") or 0),
    }


def _diff_reasons(candidate_reasons: List[str], target_reasons: List[str], limit: int = 3) -> List[str]:
    out: List[str] = []
    cset = {r.lower() for r in target_reasons}
    for r in candidate_reasons:
        if r.lower() not in cset:
            out.append(r)
        if len(out) >= limit:
            break
    if not out and candidate_reasons:
        out.append(candidate_reasons[0])
    return out


def _pick_upgrade_target(
    peers: List[Dict[str, Any]],
    candidate_fit: float,
    remaining_budget: float,
) -> Optional[Dict[str, Any]]:
    """Choose squad member most likely 'displaced' by candidate in same role bucket."""
    if not peers:
        return None
    replaceable = [p for p in peers if not p.get("_protected")]
    if not replaceable:
        return None

    if remaining_budget < 20:
        # Tight purse — worst value among lower-fit peers
        return min(
            replaceable,
            key=lambda p: (p["csk_fit"], -float(p.get("price_cr") or 0)),
        )

    return min(replaceable, key=lambda p: float(p.get("csk_fit") or 100))


def squad_impact_insight(
    conn: sqlite3.Connection,
    candidate_name: str,
    squad: List[Dict[str, Any]],
    *,
    budget: float = IPL_PURSE_CR,
    candidate_price_cr: Optional[float] = None,
    metadata_fn: Optional[MetadataFn] = None,
) -> Dict[str, Any]:
    """
    Compare auction-pool candidate vs current squad.

    squad rows: { name, role, price, country, retained?, price_verified?, droppable? }
    """
    meta_fn = metadata_fn or (
        lambda db, search=None: fetch_player_metadata(
            db,
            search_name=search,
            groq_api_key=None,
        )
    )

    role_engine = RoleInferenceEngine()
    csk_engine = CSKFitEngine()
    engine = FranchiseValuationEngine(conn)

    cand_key = _norm_name(candidate_name)
    for row in squad:
        if _norm_name(row.get("name") or row.get("player_name")) == cand_key:
            return {
                "mode": "blocked",
                "summary": f"{candidate_name.strip()} is already in your squad.",
                "candidate": {"name": candidate_name.strip()},
                "target": None,
                "deltas": {},
                "reasons": ["Already in squad"],
                "gaps": squad_gap_from_list(squad, budget=budget),
            }

    squad_state = squad_gap_from_list(squad, budget=budget)
    remaining = float(squad_state.get("remaining_budget_cr") or budget)

    valuation = None
    candidate_profile = _light_profile(
        conn,
        candidate_name,
        role_engine=role_engine,
        csk_engine=csk_engine,
        metadata_fn=meta_fn,
        price_cr=candidate_price_cr or 0,
    )

    try:
        stats = get_player_stats(conn, candidate_name)
        if stats:
            player_meta = meta_fn(stats["player_name"], candidate_name)
            valuation = engine.valuate(stats, player_meta).to_api_dict()
    except Exception:
        valuation = None

    if candidate_profile:
        if valuation:
            candidate_profile["csk_fit"] = round(float(valuation.get("csk_fit_score") or candidate_profile["csk_fit"]), 1)
            candidate_profile["form"] = round(float(valuation.get("form_score") or candidate_profile["form"]), 1)
            candidate_profile["fmv_cr"] = round(float(valuation.get("estimated_value") or 0), 2)
            candidate_profile["verdict"] = valuation.get("auction_verdict")
            candidate_profile["confidence"] = valuation.get("confidence")
        else:
            candidate_profile["fmv_cr"] = round(float(candidate_price_cr or 2), 2)
    else:
        candidate_profile = {
            "name": candidate_name.strip(),
            "display_name": candidate_name.strip(),
            "role": "Player",
            "csk_fit": 50.0,
            "form": 50.0,
            "fmv_cr": round(float(candidate_price_cr or 2), 2),
            "price_cr": round(float(candidate_price_cr or 2), 2),
            "reasons": ["Limited stats in database — pool-only estimate"],
            "matches_played": 0,
        }

    add_price = float(candidate_price_cr if candidate_price_cr is not None else candidate_profile.get("fmv_cr") or 2)

    squad_profiles: List[Dict[str, Any]] = []
    for row in squad:
        nm = (row.get("name") or row.get("player_name") or "").strip()
        if not nm:
            continue
        prof = _light_profile(
            conn,
            nm,
            role_engine=role_engine,
            csk_engine=csk_engine,
            metadata_fn=meta_fn,
            price_cr=float(row.get("price") or 0),
        )
        if not prof:
            prof = {
                "name": nm,
                "display_name": nm,
                "role": normalize_role(row.get("role") or "Player"),
                "csk_fit": 50.0,
                "form": 50.0,
                "reasons": [],
                "price_cr": round(float(row.get("price") or 0), 2),
                "matches_played": 0,
            }
        prof["_protected"] = _is_protected_replace_target(row, prof)
        prof["_row"] = row
        squad_profiles.append(prof)

    cand_role = normalize_role(
        candidate_profile.get("role")
        or (valuation.get("role") if valuation else "Player")
    )

    role_gap = next((g for g in squad_state.get("gaps", []) if g.get("role") == cand_role), None)
    need = int(role_gap.get("need") or 0) if role_gap else 0
    have = int(role_gap.get("have") or 0) if role_gap else squad_state.get("role_counts", {}).get(cand_role, 0)

    peers = [p for p in squad_profiles if normalize_role(p.get("role")) == cand_role]

    cand_stats = get_player_stats(conn, candidate_name)
    cand_country = (cand_stats or {}).get("country", "")
    if squad_state.get("overseas", 0) >= MAX_OVERSEAS and cand_stats and _is_overseas(cand_country):
        return {
            "mode": "blocked",
            "summary": "Overseas slots full (8/8)",
            "candidate": candidate_profile,
            "target": None,
            "deltas": {},
            "reasons": ["No overseas slots left"],
            "gaps": squad_state,
        }

    if len(squad) >= 25:
        return {
            "mode": "blocked",
            "summary": "Squad full (25/25) — release someone first",
            "candidate": candidate_profile,
            "target": None,
            "deltas": {},
            "reasons": ["Squad at IPL limit"],
            "gaps": squad_state,
        }

    reasons: List[str] = []
    target: Optional[Dict[str, Any]] = None
    mode = "flex_add"
    deltas: Dict[str, Any] = {}

    if need > 0:
        mode = "fill_gap"
        reasons.append(f"Fills {cand_role} gap ({have}/{IDEAL_ROLE.get(cand_role, '?')} → {have + 1})")
        reasons.extend(_diff_reasons(candidate_profile.get("reasons", []), [], 2))
        summary = (
            f"Adds {cand_role} — you need {need} more; CSK fit {candidate_profile['csk_fit']:.0f}%"
        )
    elif peers:
        mode = "upgrade"
        pick = _pick_upgrade_target(peers, candidate_profile["csk_fit"], remaining)
        if pick:
            target = {
                "name": pick["display_name"],
                "db_name": pick["name"],
                "role": pick["role"],
                "csk_fit": pick["csk_fit"],
                "form": pick["form"],
                "price_cr": pick["price_cr"],
                "protected": pick.get("_protected", False),
            }
            deltas = {
                "fit": round(candidate_profile["csk_fit"] - pick["csk_fit"], 1),
                "form": round(candidate_profile["form"] - pick["form"], 1),
                "price_cr": round(add_price - pick["price_cr"], 2),
            }
            reasons.extend(_diff_reasons(candidate_profile.get("reasons", []), pick.get("reasons", []), 3))
            if deltas["fit"] >= 8:
                summary = (
                    f"Likely upgrade over {pick['display_name']} — +{deltas['fit']:.0f} CSK fit"
                )
            elif deltas["fit"] > 0:
                summary = (
                    f"Slight upgrade over {pick['display_name']} — +{deltas['fit']:.0f} CSK fit"
                )
            else:
                summary = (
                    f"Same role as {pick['display_name']} — similar fit; compare price (Δ ₹{deltas['price_cr']:+.1f} Cr)"
                )
                mode = "marginal"
        else:
            mode = "flex_add"
            reasons.append(f"{cand_role} full — all slots protected or core players")
            summary = f"Adds to flex — {cand_role} slots full with retained players"
    else:
        mode = "flex_add"
        reasons.append(f"No same-role peer — lists as {cand_role} / flex")
        reasons.extend(_diff_reasons(candidate_profile.get("reasons", []), [], 2))
        summary = f"New {cand_role} profile for CSK — no direct replacement in squad"

    if add_price > remaining + 0.01:
        return {
            "mode": "blocked",
            "summary": f"Need ₹{add_price:.1f} Cr — only ₹{remaining:.1f} Cr left",
            "candidate": candidate_profile,
            "target": target,
            "deltas": deltas,
            "reasons": [f"Over budget by ₹{add_price - remaining:.1f} Cr"],
            "gaps": squad_state,
        }

    return {
        "mode": mode,
        "summary": summary,
        "candidate": candidate_profile,
        "target": target,
        "deltas": deltas,
        "reasons": reasons[:6],
        "gaps": {
            "role": cand_role,
            "need": need,
            "have": have,
            "ideal": IDEAL_ROLE.get(cand_role),
        },
        "purse_after_cr": round(remaining - add_price, 2),
        "add_price_cr": round(add_price, 2),
    }
