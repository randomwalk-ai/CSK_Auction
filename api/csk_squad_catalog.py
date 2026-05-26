"""
Official CSK IPL 2026 squad catalog (post mega + mini auction).

Roster source: published IPL 2026 squad lists (Sportstar / The Hindu).
Local DB (through 2025) is NOT used to decide who is in the squad — only optional
2026 bid-history prices and player stats (via valuation API) elsewhere.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple, Optional

from squad_price_resolver import resolve_player_price, refresh_verified_price_index

# 25-man CSK squad after IPL 2026 mini-auction
CSK_2026_OFFICIAL_SQUAD: List[Dict[str, Any]] = [
    {"name": "Ruturaj Gaikwad", "role": "Batter", "country": "India", "price": 16.0, "retained": True, "acquisition": "retained"},
    {"name": "Sanju Samson", "role": "Wicket Keeper", "country": "India", "price": 18.0, "retained": True, "acquisition": "trade"},
    {"name": "MS Dhoni", "role": "Wicket Keeper", "country": "India", "price": 4.0, "retained": True, "acquisition": "retained"},
    {"name": "Dewald Brevis", "role": "Batter", "country": "South Africa", "price": 4.0, "retained": True, "acquisition": "retained"},
    {"name": "Ayush Mhatre", "role": "Batter", "country": "India", "price": 0.4, "retained": True, "acquisition": "retained"},
    {"name": "Urvil Patel", "role": "Wicket Keeper", "country": "India", "price": 0.3, "retained": True, "acquisition": "retained"},
    {"name": "Shivam Dube", "role": "All Rounder", "country": "India", "price": 12.0, "retained": True, "acquisition": "retained"},
    {"name": "Jamie Overton", "role": "All Rounder", "country": "England", "price": 1.5, "retained": True, "acquisition": "retained"},
    {"name": "Ramakrishna Ghosh", "role": "All Rounder", "country": "India", "price": 0.3, "retained": True, "acquisition": "retained"},
    {"name": "Noor Ahmad", "role": "Bowler", "country": "Afghanistan", "price": 10.0, "retained": True, "acquisition": "retained"},
    {"name": "Khaleel Ahmed", "role": "Bowler", "country": "India", "price": 4.8, "retained": True, "acquisition": "retained"},
    {"name": "Anshul Kamboj", "role": "All Rounder", "country": "India", "price": 3.4, "retained": True, "acquisition": "retained"},
    {"name": "Gurjapneet Singh", "role": "Bowler", "country": "India", "price": 2.2, "retained": True, "acquisition": "retained"},
    {"name": "Shreyas Gopal", "role": "Bowler", "country": "India", "price": 0.3, "retained": True, "acquisition": "retained"},
    {"name": "Mukesh Choudhary", "role": "Bowler", "country": "India", "price": 0.3, "retained": True, "acquisition": "retained"},
    {"name": "Nathan Ellis", "role": "Bowler", "country": "Australia", "price": 2.0, "retained": True, "acquisition": "retained"},
    {"name": "Kartik Sharma", "role": "Wicket Keeper", "country": "India", "price": 14.2, "retained": False, "acquisition": "auction"},
    {"name": "Prashant Veer", "role": "All Rounder", "country": "India", "price": 14.2, "retained": False, "acquisition": "auction"},
    {"name": "Rahul Chahar", "role": "Bowler", "country": "India", "price": 5.2, "retained": False, "acquisition": "auction"},
    {"name": "Akeal Hosein", "role": "Bowler", "country": "West Indies", "price": 2.0, "retained": False, "acquisition": "auction"},
    {"name": "Matt Henry", "role": "Bowler", "country": "New Zealand", "price": 2.0, "retained": False, "acquisition": "auction"},
    {"name": "Matthew Short", "role": "All Rounder", "country": "Australia", "price": 1.5, "retained": False, "acquisition": "auction"},
    {"name": "Aman Khan", "role": "All Rounder", "country": "India", "price": 0.4, "retained": False, "acquisition": "auction"},
    {"name": "Sarfaraz Khan", "role": "Batter", "country": "India", "price": 0.75, "retained": False, "acquisition": "auction"},
    {"name": "Zakary Foulkes", "role": "All Rounder", "country": "New Zealand", "price": 0.75, "retained": False, "acquisition": "auction"},
]

# Released / not on IPL 2026 CSK roster — block if old DB or Groq tries to inject them
CSK_2026_RELEASED_OR_GONE: frozenset = frozenset(
    n.lower()
    for n in (
        "Ravindra Jadeja",
        "Matheesha Pathirana",
        "Rachin Ravindra",
        "Sam Curran",
        "Ravichandran Ashwin",
        "Devon Conway",
        "Deepak Chahar",
        "Maheesh Theekshana",
    )
)

OFFICIAL_ROSTER_NAMES: frozenset = frozenset(p["name"].lower() for p in CSK_2026_OFFICIAL_SQUAD)

CATALOG_SOURCE = "Sportstar / The Hindu IPL 2026 CSK squad lists"


def _norm(name: str) -> str:
    return (name or "").strip().lower()


def _is_overseas(country: str) -> bool:
    c = (country or "India").strip().lower()
    return c not in ("", "india", "indian", "ind")


def build_squad_from_catalog(
    conn,
    catalog: List[Dict],
    auction_year: int = 2026,
    groq_api_key: Optional[str] = None,
) -> Tuple[List[Dict], List[Dict]]:
    """
    Official roster + price chain: 2026 scrape/DB → Groq public lookup for gaps.
    """
    refresh_verified_price_index(conn, auction_year)
    squad_out: List[Dict] = []
    provenance: List[Dict] = []

    # Pass 1: scrape / DB prices
    pending_groq: List[Dict] = []
    resolved_map: Dict[str, Dict] = {}

    for player in catalog:
        resolved = resolve_player_price(
            conn,
            player["name"],
            role=player.get("role") or "",
            acquisition=player.get("acquisition") or "retained",
            year=auction_year,
            groq_api_key=None,
            use_groq=False,
        )
        resolved_map[_norm(player["name"])] = (player, resolved)
        if not resolved.get("price") and groq_api_key:
            pending_groq.append(player)

    # Pass 2: batch Groq for gaps
    groq_batch: Dict[str, Dict] = {}
    if groq_api_key and pending_groq:
        from groq_player_price import fetch_missing_prices_batch_via_groq

        groq_batch = fetch_missing_prices_batch_via_groq(
            groq_api_key, pending_groq, year=auction_year
        )

    # Pass 3: per-player Groq for anyone batch missed
    if groq_api_key:
        from groq_player_price import fetch_ip2026_csk_price_via_groq

        for player in catalog:
            key = _norm(player["name"])
            _, resolved = resolved_map[key]
            if resolved.get("price") or groq_batch.get(key, {}).get("price_cr"):
                continue
            groq_one = fetch_ip2026_csk_price_via_groq(
                groq_api_key,
                player["name"],
                role=player.get("role") or "",
                acquisition=player.get("acquisition") or "retained",
                year=auction_year,
            )
            if groq_one.get("found") and groq_one.get("price_cr"):
                groq_batch[key] = groq_one

    for player in catalog:
        player, resolved = resolved_map[_norm(player["name"])]
        if not resolved.get("price"):
            hit = groq_batch.get(_norm(player["name"]))
            if hit and hit.get("price_cr"):
                resolved = {
                    "price": hit["price_cr"],
                    "price_verified": False,
                    "price_estimated": True,
                    "price_source": "groq_public",
                    "price_confidence": hit.get("confidence") or "medium",
                    "price_note": hit.get("source_note") or "Groq IPL 2026 lookup",
                }
            else:
                resolved = resolve_player_price(
                    conn,
                    player["name"],
                    role=player.get("role") or "",
                    acquisition=player.get("acquisition") or "retained",
                    year=auction_year,
                    groq_api_key=None,
                    use_groq=False,
                    catalog_price=player.get("price"),
                )

        evidence = [f"IPL {auction_year} CSK official roster ({CATALOG_SOURCE})"]
        if resolved.get("price_note"):
            evidence.append(resolved["price_note"])

        entry = {
            "name": player["name"],
            "role": player["role"],
            "country": player["country"],
            "nationality": player["country"],
            "price": resolved["price"],
            "price_verified": resolved["price_verified"],
            "price_estimated": resolved.get("price_estimated", False),
            "price_source": resolved.get("price_source"),
            "price_confidence": resolved.get("price_confidence"),
            "price_note": resolved.get("price_note"),
            "retained": bool(player.get("retained")),
            "acquisition": player.get("acquisition", "retained" if player.get("retained") else "auction"),
            "overseas": _is_overseas(player.get("country", "India")),
            "source": "official_2026_roster",
        }
        squad_out.append(entry)
        provenance.append(
            {
                "name": player["name"],
                "verification_tier": "official_roster",
                "verified": True,
                "price_verified": resolved["price_verified"],
                "price_estimated": resolved.get("price_estimated", False),
                "confidence_pct": 95 if resolved["price_verified"] else (75 if resolved.get("price_estimated") else 50),
                "match_method": resolved.get("price_source") or "no_price",
                "evidence": evidence,
                "flags": [] if resolved.get("price") else ["No price from scrape, Groq, or press list"],
            }
        )

    squad_out.sort(
        key=lambda p: (p.get("price") or 0),
        reverse=True,
    )
    return squad_out, provenance


def build_catalog_xai(squad: List[Dict], provenance: List[Dict]) -> Dict:
    bid_confirmed = sum(1 for p in squad if p.get("price_verified"))
    groq_est = sum(1 for p in squad if p.get("price_estimated") and p.get("price_source") == "groq_public")
    press_est = sum(1 for p in squad if p.get("price_estimated") and p.get("price_source") == "press_catalog")
    no_price = sum(1 for p in squad if not p.get("price"))
    confidences: List[float] = []
    for p in squad:
        if p.get("price_verified"):
            confidences.append(95.0)
        elif p.get("price_source") == "groq_public":
            confidences.append(70.0)
        elif p.get("price_estimated"):
            confidences.append(55.0)
        elif p.get("price"):
            confidences.append(50.0)
    avg_conf = round(sum(confidences) / len(confidences), 1) if confidences else 0.0
    return {
        "title": "Squad source — IPL 2026 official roster",
        "model": "official_catalog",
        "prompt_version": "csk_squad_catalog_v4",
        "grounding": {
            "roster_source": CATALOG_SOURCE,
            "squad_size": len(squad),
            "verified_auction_prices": bid_confirmed,
            "verified_in_db": bid_confirmed,
            "groq_estimated_prices": groq_est,
            "press_estimated_prices": press_est,
            "prices_missing": no_price,
            "avg_confidence_pct": avg_conf,
            "db_anchors": bid_confirmed,
            "hallucination_risk": "low" if no_price == 0 else "medium",
        },
        "decision_trace": [
            f"Squad roster = official IPL 2026 CSK list ({len(squad)} players).",
            f"{bid_confirmed} prices from 2026 Cricbuzz scrape / bid_history (verified).",
            f"{groq_est} prices from Groq per-player IPL 2026 lookup (estimated).",
            f"{press_est} prices from Sportstar/Hindu squad list when Groq unavailable.",
            "2025 DB retained prices are never used.",
        ],
        "hallucination_flags": [
            f"{p['name']}: Groq estimate — confirm against official IPL release"
            for p in squad
            if p.get("price_estimated")
        ][:10],
        "anti_hallucination_policy": (
            "Scrape-verified prices override Groq. Groq fills gaps only when scrape has no row. "
            "Refresh prices: python scripts/seed_csk_2026_prices.py --import-db "
            "and python scripts/scrape_cricbuzz_all_bids.py --years 2026 --teams CSK --import-db"
        ),
    }


def load_official_csk_squad_2026(
    conn,
    auction_year: int = 2026,
    groq_api_key: Optional[str] = None,
) -> Dict[str, Any]:
    squad, provenance = build_squad_from_catalog(
        conn, CSK_2026_OFFICIAL_SQUAD, auction_year, groq_api_key=groq_api_key
    )
    xai = build_catalog_xai(squad, provenance)
    return {
        "squad": squad,
        "count": len(squad),
        "source": "official_catalog_2026",
        "note": (
            f"Official IPL {auction_year} CSK squad ({len(squad)} players). "
            "Not built from 2025 DB retained list."
        ),
        "xai": xai,
        "player_provenance": provenance,
    }


def fetch_full_csk_squad_2026(
    conn,
    groq_api_key: Optional[str] = None,
    *,
    prefer_groq: bool = False,
    auction_year: int = 2026,
) -> Dict[str, Any]:
    """
    Return the official 25-man IPL 2026 CSK squad.
    Groq does not change roster membership — catalog is authoritative.
    """
    result = load_official_csk_squad_2026(
        conn,
        auction_year=auction_year,
        groq_api_key=groq_api_key,
    )
    priced = sum(1 for p in result["squad"] if p.get("price"))
    verified = sum(1 for p in result["squad"] if p.get("price_verified"))
    if groq_api_key:
        result["note"] = (
            f"Official IPL {auction_year} CSK squad — "
            f"{priced}/{len(result['squad'])} priced ({verified} verified, "
            f"{priced - verified} estimated)."
        )
    elif priced == len(result["squad"]):
        result["note"] = (
            f"Official IPL {auction_year} CSK squad — all {priced} prices from "
            "2026 scrape / official squad DB."
        )
    return result


def filter_to_official_roster(squad: List[Dict]) -> List[Dict]:
    """Drop any player not on the official 2026 list (stale DB / cache safety)."""
    return [
        p for p in squad
        if (p.get("name") or "").strip().lower() in OFFICIAL_ROSTER_NAMES
        and (p.get("name") or "").strip().lower() not in CSK_2026_RELEASED_OR_GONE
    ]
