"""
CSK squad via Groq with DB grounding and XAI observability.

Flow:
1. Collect verified anchors from local auction DB (retained + bid wins).
2. Ask Groq for the full IPL squad roster (up to 25) with structured JSON.
3. Cross-validate every player against bid_history, auction_prices_full, player stats.
4. Return squad + xai block so the UI can show trust / hallucination flags.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

import httpx

from player_loader import find_player_by_fuzzy_name, normalize_player_name

logger = logging.getLogger(__name__)

PROMPT_VERSION = "csk_squad_v1"
DEFAULT_MODEL = os.getenv("GROQ_SQUAD_MODEL", "llama-3.3-70b-versatile")
MAX_SQUAD = 25


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _normalize_role(role: str) -> str:
    r = (role or "").strip().lower()
    if "wk" in r or "wicket" in r:
        return "Wicket Keeper"
    if "all" in r or "ar" in r:
        return "All Rounder"
    if "bowl" in r:
        return "Bowler"
    if "bat" in r:
        return "Batter"
    return role.strip() or "Player"


def collect_db_anchors(conn, auction_year: int = 2026) -> Tuple[List[Dict], Dict[str, Dict]]:
    """Verified CSK players from local DB — used as grounding for Groq."""
    prior_year = auction_year - 1
    anchors: List[Dict] = []
    by_key: Dict[str, Dict] = {}

    retained = conn.execute(
        """
        SELECT player_name, role, country, price
        FROM auction_prices_full
        WHERE team_code = 'CSK' AND year = ? AND status = 'retained' AND price IS NOT NULL
        ORDER BY price DESC
        """,
        (prior_year,),
    ).fetchall()

    for row in retained:
        name = (row[0] or "").strip()
        if not name:
            continue
        entry = {
            "name": name,
            "role": _normalize_role(row[1] or ""),
            "country": row[2] or "India",
            "price": round(float(row[3] or 0), 2),
            "retained": True,
            "anchor_source": "auction_prices_retained",
            "auction_year": prior_year,
        }
        anchors.append(entry)
        by_key[_norm(name)] = entry

    wins = conn.execute(
        """
        SELECT player_name, role, country, last_bid_cr
        FROM bid_history
        WHERE viewing_team_code = 'CSK' AND year = ? AND bid_war = 'won'
        ORDER BY last_bid_cr DESC
        """,
        (auction_year,),
    ).fetchall()

    for row in wins:
        name = (row[0] or "").strip()
        if not name:
            continue
        key = _norm(name)
        if key in by_key:
            continue
        entry = {
            "name": name,
            "role": _normalize_role(row[1] or ""),
            "country": row[2] or "India",
            "price": round(float(row[3] or 0), 2),
            "retained": False,
            "anchor_source": "bid_history_won",
            "auction_year": auction_year,
        }
        anchors.append(entry)
        by_key[key] = entry

    return anchors, by_key


def _lookup_csk_auction_row(conn, name: str, year: int) -> Optional[Dict]:
    """Match player to CSK auction/bid rows for a given year or prior retained."""
    fuzzy = find_player_by_fuzzy_name(conn, name)
    search_names = [name]
    if fuzzy and fuzzy not in search_names:
        search_names.append(fuzzy)

    for candidate in search_names:
        row = conn.execute(
            """
            SELECT player_name, role, country, price, status, year
            FROM auction_prices_full
            WHERE team_code = 'CSK' AND player_name = ? AND year IN (?, ?)
            ORDER BY year DESC
            LIMIT 1
            """,
            (candidate, year, year - 1),
        ).fetchone()
        if row:
            return {
                "db_name": row[0],
                "role": row[1],
                "country": row[2],
                "price": round(float(row[3] or 0), 2),
                "retained": row[4] == "retained",
                "source": "auction_prices_full",
                "year": row[5],
            }

        bid = conn.execute(
            """
            SELECT player_name, role, country, last_bid_cr, bid_war
            FROM bid_history
            WHERE viewing_team_code = 'CSK' AND player_name = ? AND year = ?
            LIMIT 1
            """,
            (candidate, year),
        ).fetchone()
        if bid:
            return {
                "db_name": bid[0],
                "role": bid[1],
                "country": bid[2],
                "price": round(float(bid[3] or 0), 2),
                "retained": False,
                "source": "bid_history",
                "year": year,
                "bid_war": bid[4],
            }

    return None


def validate_player(
    conn,
    player: Dict,
    auction_year: int,
    anchors_by_key: Dict[str, Dict],
) -> Dict:
    """Attach XAI provenance and verification tier to one squad member."""
    name = (player.get("name") or "").strip()
    key = _norm(name)
    anchor = anchors_by_key.get(key)

    if not anchor:
        for ak, av in anchors_by_key.items():
            if _similarity(name, av["name"]) >= 0.88:
                anchor = av
                key = ak
                break

    db_row = _lookup_csk_auction_row(conn, name, auction_year)
    stats_name = find_player_by_fuzzy_name(conn, name)
    stats_match_score = _similarity(name, stats_name) if stats_name else 0.0

    groq_price = round(float(player.get("price") or player.get("price_cr") or 0), 2)
    groq_role = _normalize_role(player.get("role") or "")
    groq_country = player.get("country") or player.get("nationality") or "India"
    groq_retained = bool(player.get("retained"))

    evidence: List[str] = []
    flags: List[str] = []

    if anchor:
        tier = "db_anchor"
        verified = True
        final_name = anchor["name"]
        final_price = anchor["price"]
        final_role = anchor["role"]
        final_country = anchor["country"]
        final_retained = anchor["retained"]
        evidence.append(
            f"DB anchor ({anchor['anchor_source']}): {final_name} @ ₹{final_price} Cr"
        )
        if groq_price and abs(groq_price - final_price) > 0.25:
            flags.append(
                f"Price mismatch: Groq said ₹{groq_price} Cr, DB has ₹{final_price} Cr — using DB"
            )
    elif db_row:
        tier = "db_confirmed"
        verified = True
        final_name = db_row["db_name"]
        final_price = db_row["price"]
        final_role = _normalize_role(db_row.get("role") or groq_role)
        final_country = db_row.get("country") or groq_country
        final_retained = db_row.get("retained", groq_retained)
        evidence.append(
            f"Matched {db_row['source']} ({db_row.get('year')}): {final_name} @ ₹{final_price} Cr"
        )
        if groq_price and abs(groq_price - final_price) > 0.5:
            flags.append(f"Groq price ₹{groq_price} Cr adjusted to DB ₹{final_price} Cr")
    elif stats_name and stats_match_score >= 0.75:
        tier = "player_db_only"
        verified = False
        final_name = stats_name
        final_price = groq_price
        final_role = groq_role
        final_country = groq_country
        final_retained = groq_retained
        evidence.append(
            f"Player exists in stats DB ({stats_name}) but no CSK auction row — roster unconfirmed"
        )
        flags.append("Not found in CSK auction/bid tables — possible hallucination or missing scrape")
    else:
        tier = "unverified"
        verified = False
        final_name = name
        final_price = groq_price
        final_role = groq_role
        final_country = groq_country
        final_retained = groq_retained
        evidence.append("Groq-only: no match in local auction DB or player stats")
        flags.append("High hallucination risk — exclude from trusted count")

    overseas = str(final_country).lower() not in ("", "india", "indian", "ind")

    confidence = {
        "db_anchor": 98,
        "db_confirmed": 92,
        "player_db_only": 45,
        "unverified": 15,
    }.get(tier, 30)

    return {
        "name": final_name,
        "role": final_role,
        "country": final_country,
        "nationality": final_country,
        "price": final_price,
        "retained": final_retained,
        "overseas": overseas,
        "xai": {
            "verification_tier": tier,
            "verified": verified,
            "confidence_pct": confidence,
            "match_method": anchor["anchor_source"] if anchor else (db_row or {}).get("source", "groq_only"),
            "evidence": evidence,
            "flags": flags,
            "groq_original_name": name,
            "groq_price_cr": groq_price,
        },
    }


def _build_groq_prompt(anchors: List[Dict], auction_year: int) -> str:
    from csk_squad_catalog import CSK_2026_OFFICIAL_SQUAD, CATALOG_SOURCE

    official = CSK_2026_OFFICIAL_SQUAD if auction_year == 2026 else []
    evidence = {
        "auction_year": auction_year,
        "team": "Chennai Super Kings (CSK)",
        "max_squad_size": MAX_SQUAD,
        "verified_db_players": anchors,
        "official_published_roster": official,
        "catalog_source": CATALOG_SOURCE,
        "instruction": (
            "Return the COMPLETE CSK IPL squad after the 2026 mega + mini auction. "
            "Must include ALL official_published_roster players and verified_db_players. "
            "Jadeja and Pathirana were RELEASED — do NOT include them. "
            "Sanju Samson was traded IN. Max 25 players."
        ),
    }
    return f"""You are an IPL auction data assistant. Use ONLY real CSK IPL {auction_year} squad facts.

VERIFIED DATABASE ANCHORS (must appear in your answer):
{json.dumps(evidence, indent=2)}

Return ONLY valid JSON (no markdown):
{{
  "squad": [
    {{
      "name": "Full Player Name",
      "role": "Batter|Bowler|All Rounder|Wicket Keeper",
      "country": "India",
      "price_cr": 12.5,
      "retained": true,
      "acquisition": "retained|auction|rtm",
      "source_note": "one line why this player is on CSK"
    }}
  ],
  "reasoning_summary": "2-3 sentences on how you built the 25-man list",
  "data_sources": ["list of sources you relied on"]
}}"""


def _call_groq(
    groq_api_key: str,
    prompt: str,
    model: str = DEFAULT_MODEL,
) -> Dict:
    with httpx.Client(timeout=45.0) as client:
        resp = client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {groq_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 3500,
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        content = re.sub(r"```json|```", "", content).strip()
        return json.loads(content)


def _merge_missing_anchors(validated: List[Dict], anchors: List[Dict]) -> List[Dict]:
    """Ensure DB anchors are never dropped by Groq omissions."""
    present = {_norm(p["name"]) for p in validated}
    merged = list(validated)
    for anchor in anchors:
        if _norm(anchor["name"]) not in present:
            merged.append(
                {
                    "name": anchor["name"],
                    "role": anchor["role"],
                    "country": anchor["country"],
                    "nationality": anchor["country"],
                    "price": anchor["price"],
                    "retained": anchor["retained"],
                    "overseas": str(anchor["country"]).lower()
                    not in ("", "india", "indian", "ind"),
                    "xai": {
                        "verification_tier": "db_anchor",
                        "verified": True,
                        "confidence_pct": 99,
                        "match_method": anchor["anchor_source"],
                        "evidence": [
                            f"Injected missing DB anchor: {anchor['name']}"
                        ],
                        "flags": ["Groq omitted this verified DB player — auto-merged"],
                        "groq_original_name": None,
                        "groq_price_cr": anchor["price"],
                    },
                }
            )
    return merged


def _dedupe_squad(players: List[Dict]) -> List[Dict]:
    by_name: Dict[str, Dict] = {}
    for p in players:
        key = _norm(p["name"])
        if not key:
            continue
        existing = by_name.get(key)
        if not existing:
            by_name[key] = p
            continue
        # Prefer higher-confidence row
        if (p.get("xai") or {}).get("confidence_pct", 0) > (
            existing.get("xai") or {}
        ).get("confidence_pct", 0):
            by_name[key] = p
    ordered = sorted(
        by_name.values(),
        key=lambda x: (x.get("price") or 0),
        reverse=True,
    )
    return ordered[:MAX_SQUAD]


def build_xai_summary(
    squad: List[Dict],
    groq_meta: Dict,
    anchors: List[Dict],
    model: str,
) -> Dict:
    verified = sum(1 for p in squad if (p.get("xai") or {}).get("verified"))
    unverified = len(squad) - verified
    tiers: Dict[str, int] = {}
    for p in squad:
        tier = (p.get("xai") or {}).get("verification_tier", "unknown")
        tiers[tier] = tiers.get(tier, 0) + 1

    hallucination_flags = [
        f"{p['name']}: {(p.get('xai') or {}).get('flags', ['unverified'])[0]}"
        for p in squad
        if not (p.get("xai") or {}).get("verified")
    ]

    avg_conf = (
        sum((p.get("xai") or {}).get("confidence_pct", 0) for p in squad) / len(squad)
        if squad
        else 0
    )

    trace = [
        f"Grounded Groq on {len(anchors)} DB-verified CSK players (retained + auction wins).",
        f"Groq returned {groq_meta.get('raw_count', '?')} players; merged & validated to {len(squad)}.",
        f"Verified in local auction DB: {verified}/{len(squad)} ({round(100*verified/max(len(squad),1))}%).",
    ]
    if unverified:
        trace.append(
            f"{unverified} player(s) flagged — shown with warning badges; not counted as trusted."
        )
    if groq_meta.get("reasoning_summary"):
        trace.append(f"LLM reasoning: {groq_meta['reasoning_summary']}")

    risk = "low" if verified >= len(squad) * 0.85 else "medium" if verified >= len(squad) * 0.6 else "high"

    return {
        "title": "Explainable AI — Squad Provenance",
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "grounding": {
            "db_anchors": len(anchors),
            "squad_size": len(squad),
            "verified_in_db": verified,
            "unverified": unverified,
            "tier_breakdown": tiers,
            "hallucination_risk": risk,
            "avg_confidence_pct": round(avg_conf, 1),
        },
        "decision_trace": trace,
        "hallucination_flags": hallucination_flags[:12],
        "llm_data_sources": groq_meta.get("data_sources") or [],
        "llm_reasoning_summary": groq_meta.get("reasoning_summary"),
        "anti_hallucination_policy": (
            "Every player is cross-checked against bid_history, auction_prices_full, "
            "and player_auction_stats. DB prices override LLM. Unverified players are visibly flagged."
        ),
    }


def fetch_groq_roster_summary(
    groq_api_key: str,
    official_roster: List[Dict],
    auction_year: int = 2026,
    model: str = DEFAULT_MODEL,
) -> str:
    """Ask Groq to sanity-check the official roster — does NOT change player list."""
    names = [p["name"] for p in official_roster]
    prompt = f"""You are an IPL analyst. The CSK IPL {auction_year} squad is OFFICIALLY:
{json.dumps(names, indent=2)}

In 2 sentences: confirm this is post-mega-auction CSK (note Jadeja/Pathirana are NOT on this team).
Return plain text only, no JSON."""
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {groq_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 200,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()


def fetch_csk_squad_via_groq(
    conn,
    groq_api_key: str,
    auction_year: int = 2026,
    model: str = DEFAULT_MODEL,
) -> Dict[str, Any]:
    """Groq path — roster locked to official catalog; Groq adds optional summary only."""
    from csk_squad_catalog import load_official_csk_squad_2026, CSK_2026_OFFICIAL_SQUAD

    result = load_official_csk_squad_2026(conn, auction_year=auction_year)
    try:
        summary = fetch_groq_roster_summary(groq_api_key, CSK_2026_OFFICIAL_SQUAD, auction_year, model)
        result["source"] = "official_catalog_2026+groq"
        result["xai"]["llm_summary"] = summary
        result["xai"]["model"] = model
        result["note"] = (
            f"Official IPL {auction_year} CSK squad ({result['count']} players) "
            "+ Groq sanity check. Roster not taken from 2025 DB."
        )
    except Exception as e:
        logger.warning("Groq roster summary failed: %s", e)
        result["note"] += f" (Groq summary unavailable: {e})"
    return result
