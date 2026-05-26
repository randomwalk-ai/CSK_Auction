"""
Resolve CSK squad player prices.

Priority:
  1. bid_history DB — 2026 CSK auction wins (Cricbuzz scrape import)
  2. Local Cricbuzz scrape CSV (same data)
  3. auction_prices_full — CSK + year=2026 (seed: scripts/seed_csk_2026_prices.py)
  4. data/csk_2026_official_prices.csv — same seed output
  5. Groq — IPL 2026 public price lookup (estimated)
  6. Embedded Sportstar/Hindu squad list (estimated, last resort)

2025 retained DB prices are never used.
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from player_loader import find_player_by_fuzzy_name

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
BIDS_MASTER = ROOT / "data" / "cricbuzz_all_bids" / "all_bids_master.csv"
OFFICIAL_PRICES_CSV = ROOT / "data" / "csk_2026_official_prices.csv"

_price_cache: Dict[str, Any] = {"loaded_at": None, "prices": {}}


def _norm(name: str) -> str:
    return (name or "").strip().lower()


def _load_csv_auction_wins(year: int = 2026, team: str = "CSK") -> Tuple[Dict[str, Dict], float]:
    paths = []
    year_csv = ROOT / "data" / "cricbuzz_all_bids" / str(year) / f"{team}.csv"
    if year_csv.exists():
        paths.append(year_csv)
    if BIDS_MASTER.exists():
        paths.append(BIDS_MASTER)

    out: Dict[str, Dict] = {}
    latest_mtime = 0.0

    for path in paths:
        latest_mtime = max(latest_mtime, path.stat().st_mtime)
        try:
            with path.open(newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if str(row.get("year", "")).strip() != str(year):
                        continue
                    if (row.get("viewing_team_code") or row.get("team_code") or "").upper() != team:
                        continue
                    if (row.get("bid_war") or row.get("status") or "").lower() != "won":
                        continue
                    name = (row.get("player_name") or "").strip()
                    if not name:
                        continue
                    try:
                        price = round(float(row.get("last_bid_cr") or row.get("price_cr") or 0), 2)
                    except (TypeError, ValueError):
                        continue
                    if price <= 0:
                        continue
                    out[_norm(name)] = {
                        "price": price,
                        "source": "cricbuzz_scrape_csv",
                        "source_file": path.name,
                        "tier": "verified",
                    }
        except OSError as e:
            logger.warning("Could not read scrape CSV %s: %s", path, e)

    return out, latest_mtime


def _load_db_auction_wins(conn, year: int = 2026, team: str = "CSK") -> Dict[str, Dict]:
    out: Dict[str, Dict] = {}
    rows = conn.execute(
        """
        SELECT player_name, last_bid_cr, source
        FROM bid_history
        WHERE viewing_team_code = ? AND year = ? AND bid_war = 'won'
        """,
        (team, year),
    ).fetchall()
    for name, price, src in rows:
        if not name or price is None:
            continue
        out[_norm(name)] = {
            "price": round(float(price), 2),
            "source": "bid_history_db",
            "source_file": src or "bid_history",
            "tier": "verified",
        }
    return out


def _load_db_auction_prices_full(conn, year: int = 2026, team: str = "CSK") -> Dict[str, Dict]:
    out: Dict[str, Dict] = {}
    rows = conn.execute(
        """
        SELECT player_name, price, status
        FROM auction_prices_full
        WHERE team_code = ? AND year = ? AND price IS NOT NULL AND price > 0
        """,
        (team, year),
    ).fetchall()
    for name, price, status in rows:
        if not name:
            continue
        out[_norm(name)] = {
            "price": round(float(price), 2),
            "source": "auction_prices_full",
            "source_file": status or "auction",
            "tier": "verified",
        }
    return out


def _load_official_prices_csv(year: int = 2026, team: str = "CSK") -> Dict[str, Dict]:
    """data/csk_2026_official_prices.csv — seeded retention/trade + hammer prices."""
    out: Dict[str, Dict] = {}
    if not OFFICIAL_PRICES_CSV.exists():
        return out
    try:
        with OFFICIAL_PRICES_CSV.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if str(row.get("year", "")).strip() != str(year):
                    continue
                if (row.get("team_code") or team).upper() != team:
                    continue
                name = (row.get("player_name") or "").strip()
                if not name:
                    continue
                try:
                    price = round(float(row.get("price") or 0), 2)
                except (TypeError, ValueError):
                    continue
                if price <= 0:
                    continue
                out[_norm(name)] = {
                    "price": price,
                    "source": "official_2026_squad_csv",
                    "source_file": OFFICIAL_PRICES_CSV.name,
                    "tier": "verified",
                    "note": row.get("note") or "IPL 2026 official squad prices",
                }
    except OSError as e:
        logger.warning("Could not read official prices CSV %s: %s", OFFICIAL_PRICES_CSV, e)
    return out


def _load_catalog_press_prices(year: int = 2026) -> Dict[str, Dict]:
    """Embedded Sportstar/Hindu IPL 2026 squad list — last-resort before TBC."""
    if year != 2026:
        return {}
    try:
        from csk_squad_catalog import CSK_2026_OFFICIAL_SQUAD
    except ImportError:
        return {}
    out: Dict[str, Dict] = {}
    for player in CSK_2026_OFFICIAL_SQUAD:
        name = player.get("name")
        if not name:
            continue
        try:
            price = round(float(player.get("price") or 0), 2)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        out[_norm(name)] = {
            "price": price,
            "source": "press_catalog",
            "source_file": "csk_squad_catalog",
            "tier": "estimated",
            "note": f"IPL {year} Sportstar/Hindu squad list",
        }
    return out


def refresh_verified_price_index(conn, year: int = 2026, team: str = "CSK") -> Dict[str, Dict]:
    csv_prices, csv_mtime = _load_csv_auction_wins(year, team)
    db_bids = _load_db_auction_wins(conn, year, team)
    db_full = _load_db_auction_prices_full(conn, year, team)
    official_csv = _load_official_prices_csv(year, team)
    # Lowest priority first; hammer/scrape rows win over squad-list amounts
    merged = {**official_csv, **db_full, **db_bids, **csv_prices}
    _price_cache["prices"] = merged
    _price_cache["estimated"] = _load_catalog_press_prices(year)
    _price_cache["loaded_at"] = datetime.now(timezone.utc).isoformat()
    _price_cache["csv_mtime"] = csv_mtime
    return merged


def _lookup_scrape_index(conn, player_name: str, year: int, team: str) -> Optional[Dict]:
    if not _price_cache.get("prices"):
        refresh_verified_price_index(conn, year, team)
    index: Dict[str, Dict] = _price_cache["prices"]
    key = _norm(player_name)
    hit = index.get(key)
    if not hit:
        fuzzy = find_player_by_fuzzy_name(conn, player_name)
        if fuzzy:
            hit = index.get(_norm(fuzzy))
    return hit


def _lookup_estimated_index(conn, player_name: str, year: int, team: str) -> Optional[Dict]:
    if not _price_cache.get("estimated"):
        refresh_verified_price_index(conn, year, team)
    index: Dict[str, Dict] = _price_cache.get("estimated") or {}
    key = _norm(player_name)
    hit = index.get(key)
    if not hit:
        fuzzy = find_player_by_fuzzy_name(conn, player_name)
        if fuzzy:
            hit = index.get(_norm(fuzzy))
    return hit


def resolve_player_price(
    conn,
    player_name: str,
    *,
    role: str = "",
    acquisition: str = "retained",
    year: int = 2026,
    team: str = "CSK",
    groq_api_key: Optional[str] = None,
    use_groq: bool = True,
    catalog_price: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Resolve display price for one squad player.
    verified = scrape/DB 2026; estimated = Groq public lookup.
    """
    hit = _lookup_scrape_index(conn, player_name, year, team)
    if hit:
        is_hammer = hit["source"] in (
            "cricbuzz_scrape_csv",
            "bid_history_db",
        ) or hit.get("source_file") == "CSK.csv"
        return {
            "price": hit["price"],
            "price_verified": True,
            "price_estimated": False,
            "price_source": hit["source"],
            "price_confidence": "high",
            "price_note": hit.get("note")
            or (
                f"2026 Cricbuzz hammer ({hit.get('source_file', hit['source'])})"
                if is_hammer
                else f"IPL {year} official squad price ({hit.get('source_file', hit['source'])})"
            ),
        }

    if groq_api_key and use_groq:
        from groq_player_price import fetch_ip2026_csk_price_via_groq

        groq = fetch_ip2026_csk_price_via_groq(
            groq_api_key,
            player_name,
            role=role,
            acquisition=acquisition,
            team=team,
            year=year,
        )
        if groq.get("found") and groq.get("price_cr"):
            conf = groq.get("confidence") or "medium"
            return {
                "price": groq["price_cr"],
                "price_verified": False,
                "price_estimated": True,
                "price_source": "groq_public",
                "price_confidence": conf,
                "price_note": groq.get("source_note") or "Groq IPL 2026 public price lookup",
            }

    press = _lookup_estimated_index(conn, player_name, year, team)
    if press:
        return {
            "price": press["price"],
            "price_verified": False,
            "price_estimated": True,
            "price_source": press["source"],
            "price_confidence": "medium",
            "price_note": press.get("note") or f"IPL {year} Sportstar/Hindu squad list",
        }

    if catalog_price is not None:
        try:
            press_price = round(float(catalog_price), 2)
        except (TypeError, ValueError):
            press_price = 0.0
        if press_price > 0:
            return {
                "price": press_price,
                "price_verified": False,
                "price_estimated": True,
                "price_source": "press_catalog",
                "price_confidence": "medium",
                "price_note": f"IPL {year} Sportstar/Hindu squad list (Groq/scrape unavailable)",
            }

    return {
        "price": None,
        "price_verified": False,
        "price_estimated": False,
        "price_source": None,
        "price_confidence": None,
        "price_note": "No 2026 scrape row and Groq lookup unavailable",
    }


# Backward compat
def resolve_verified_price(conn, player_name: str, year: int = 2026, team: str = "CSK") -> Dict[str, Any]:
    return resolve_player_price(conn, player_name, year=year, team=team, use_groq=False)
