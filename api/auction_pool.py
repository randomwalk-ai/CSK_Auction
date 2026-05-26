"""
IPL auction player pool — full auction roster for a given year.

Primary source: auction_prices_full (Cricbuzz completed auction scrape).
Enriched with bid_history (num_bids, last_bid_cr) where available.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from war_room import load_bids_df, normalize_role

from player_loader import get_player_stats, normalize_player_name
from player_portrait_store import _normalize_key
from ipl_facecards import facecard_url_map

ROOT = Path(__file__).resolve().parent.parent
AUCTION_CSV = ROOT / "data" / "cricbuzz_auction_all_teams.csv"

STATUS_RANK = {
    "sold": 5,
    "auction_won": 5,
    "traded": 4,
    "retained": 3,
    "unsold": 2,
    "listed": 1,
    "unknown": 0,
}


def _stats_for_bid_player(conn: sqlite3.Connection, bid_name: str) -> Optional[Dict[str, Any]]:
    """Stats for an auction-pool name — rejects cross-player fuzzy matches."""
    stats = get_player_stats(conn, bid_name)
    if not stats:
        return None
    matched = str(stats.get("player_name") or "")
    bid_norm = normalize_player_name(bid_name).lower()
    matched_norm = normalize_player_name(matched).lower()
    if bid_norm == matched_norm:
        return stats
    bid_last = bid_norm.split()[-1]
    matched_last = matched_norm.split()[-1]
    if bid_last != matched_last:
        return None
    return stats


def _historical_role_mode(bid_name: str, year: int) -> Optional[str]:
    df = load_bids_df()
    if df.empty:
        return None
    hist = df[
        (df["year"] == year)
        & (df["player_name"].astype(str).str.strip().str.lower() == bid_name.strip().lower())
    ]
    if hist.empty:
        return None
    counts = hist["role"].map(normalize_role).value_counts()
    if counts.empty:
        return None
    return str(counts.index[0])


def _resolve_pool_role(name: str, auction_role: str, stats: Optional[Dict[str, Any]], year: int) -> str:
    raw = str(auction_role or "")
    if "WK" in raw.upper() or "wicket" in raw.lower():
        return "Wicket Keeper"

    bucket = normalize_role(raw)
    hist = _historical_role_mode(name, year)
    if hist and hist != "Unknown":
        bucket = hist

    if stats:
        runs = int(stats.get("total_runs") or 0)
        wkts = int(stats.get("total_wickets") or 0)
        if bucket == "Bowler" and runs >= 400 and wkts <= 5:
            bucket = "Batter"
        if bucket == "Batter" and wkts >= 35 and runs < 250:
            bucket = "Bowler"
        if runs >= 800 and wkts <= 3 and hist == "Wicket Keeper":
            bucket = "Wicket Keeper"

    return bucket or "Unknown"


def _load_auction_roster_df(conn: sqlite3.Connection, year: int) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT player_name, year, role, country, price, status, team_code, base_price_cr,
               cricbuzz_player_id, source
        FROM auction_prices_full
        WHERE year = ?
        """,
        (year,),
    ).fetchall()

    if rows:
        df = pd.DataFrame(
            rows,
            columns=[
                "player_name",
                "year",
                "role",
                "country",
                "price",
                "status",
                "team_code",
                "base_price_cr",
                "cricbuzz_player_id",
                "source",
            ],
        )
    elif AUCTION_CSV.exists():
        df = pd.read_csv(AUCTION_CSV)
        df = df[df["year"] == year].copy()
    else:
        return pd.DataFrame()

    if df.empty:
        return df

    df["player_name"] = df["player_name"].astype(str).str.strip()
    df["status"] = df["status"].fillna("unknown").astype(str).str.lower()
    df["price"] = pd.to_numeric(df.get("price", df.get("price_cr", 0)), errors="coerce").fillna(0.0)
    df["base_price_cr"] = pd.to_numeric(df.get("base_price_cr", 0), errors="coerce").fillna(0.0)
    df["country"] = df["country"].fillna("Unknown").astype(str)
    df["role_bucket"] = df["role"].map(normalize_role)
    df["status_rank"] = df["status"].map(lambda s: STATUS_RANK.get(s, 0))
    df = df.sort_values(["price", "status_rank"], ascending=False)
    df = df.drop_duplicates("player_name", keep="first")
    return df


def _load_bid_aggregate(year: int) -> pd.DataFrame:
    df = load_bids_df()
    if df.empty:
        return pd.DataFrame()
    yr = df[df["year"] == year].copy()
    if yr.empty:
        return pd.DataFrame()

    agg = yr.groupby("player_name", as_index=False).agg(
        num_bids=("num_bids", "max"),
        last_bid_cr=("last_bid_cr", "max"),
        bid_role=("role", "first"),
        bid_country=("country", "first"),
        bid_won=("won", "max"),
    )
    return agg


def _pool_frame(conn: sqlite3.Connection, year: int) -> pd.DataFrame:
    roster = _load_auction_roster_df(conn, year)
    bids = _load_bid_aggregate(year)

    if roster.empty and bids.empty:
        return pd.DataFrame()

    if roster.empty:
        yr = bids.copy()
        yr["role"] = yr["bid_role"]
        yr["country"] = yr["bid_country"]
        yr["price"] = yr["last_bid_cr"]
        yr["base_price_cr"] = 0.0
        yr["status"] = yr["bid_won"].map(lambda w: "sold" if w else "unsold")
        yr["team_code"] = ""
        yr["role_bucket"] = yr["role"].map(normalize_role)
        return yr.sort_values(["num_bids", "last_bid_cr"], ascending=False).drop_duplicates("player_name")

    merged = roster.merge(bids, on="player_name", how="left", suffixes= ("", "_bid"))

    if not bids.empty:
        missing = bids[~bids["player_name"].isin(roster["player_name"])].copy()
        if not missing.empty:
            extra = pd.DataFrame(
                {
                    "player_name": missing["player_name"],
                    "year": year,
                    "role": missing["bid_role"],
                    "country": missing["bid_country"],
                    "price": missing["last_bid_cr"],
                    "status": missing["bid_won"].map(lambda w: "sold" if w else "unsold"),
                    "team_code": "",
                    "base_price_cr": 0.0,
                    "cricbuzz_player_id": "",
                    "source": "bid_history",
                    "role_bucket": missing["bid_role"].map(normalize_role),
                    "num_bids": missing["num_bids"],
                    "last_bid_cr": missing["last_bid_cr"],
                    "bid_role": missing["bid_role"],
                    "bid_country": missing["bid_country"],
                    "bid_won": missing["bid_won"],
                }
            )
            merged = pd.concat([merged, extra], ignore_index=True)

    merged["role_bucket"] = merged["role"].map(normalize_role)
    merged["num_bids"] = pd.to_numeric(merged.get("num_bids", 0), errors="coerce").fillna(0).astype(int)
    merged["last_bid_cr"] = pd.to_numeric(merged.get("last_bid_cr", 0), errors="coerce").fillna(0.0)
    if "bid_won" in merged.columns:
        merged["bid_won"] = merged["bid_won"].map(lambda v: bool(v) if pd.notna(v) else False)
    else:
        merged["bid_won"] = False
    return merged.drop_duplicates("player_name", keep="first")


def _bubble_price(row: pd.Series) -> float:
    bid_cr = round(float(row.get("last_bid_cr") or 0), 2)
    if bid_cr > 0:
        return bid_cr
    price = round(float(row.get("price") or 0), 2)
    if price > 0:
        return price
    base = round(float(row.get("base_price_cr") or 0), 2)
    if base > 0:
        return base
    return 2.0


def auction_pool_players(
    conn: sqlite3.Connection,
    *,
    year: int = 2026,
    pool_filter: str = "batters",
    limit: int = 48,
) -> List[Dict[str, Any]]:
    """
    Return stat rows for players in the year's full auction pool.

    pool_filter: batters | bowlers | allrounders | inform | all
    """
    yr = _pool_frame(conn, year)
    if yr.empty:
        return []

    filt = (pool_filter or "batters").strip().lower()
    if filt == "batters":
        yr = yr[yr["role_bucket"] == "Batter"]
    elif filt == "bowlers":
        yr = yr[yr["role_bucket"] == "Bowler"]
    elif filt == "allrounders":
        yr = yr[yr["role_bucket"] == "All Rounder"]
    elif filt == "inform":
        pass
    elif filt != "all":
        yr = yr[yr["role_bucket"] == "Batter"]

    names = [str(r["player_name"]).strip() for _, r in yr.iterrows()]
    facecards = facecard_url_map(conn, names)

    out: List[Dict[str, Any]] = []
    for _, row in yr.iterrows():
        name = str(row["player_name"]).strip()
        stats = _stats_for_bid_player(conn, name)
        auction_role = str(row.get("role") or "")
        pool_role = _resolve_pool_role(name, auction_role, stats, year)
        bid_cr = round(float(row.get("last_bid_cr") or 0), 2)
        hammer = round(float(row.get("price") or 0), 2)
        bubble = _bubble_price(row)
        rec = {
            "player_name": name,
            "matches_played": stats.get("matches_played") if stats else None,
            "total_runs": (stats.get("total_runs") or 0) if stats else 0,
            "total_wickets": (stats.get("total_wickets") or 0) if stats else 0,
            "average": stats.get("average") if stats else None,
            "strike_rate": stats.get("strike_rate") if stats else None,
            "economy_rate": stats.get("economy_rate") if stats else None,
            "form_rating": stats.get("form_rating") if stats else None,
            "country": (stats.get("country") if stats else None) or row.get("country") or "Unknown",
            "pool_role": pool_role,
            "auction_role": auction_role,
            "auction_year": year,
            "auction_status": str(row.get("status") or "unknown"),
            "last_bid_cr": bid_cr if bid_cr > 0 else None,
            "hammer_price_cr": hammer if hammer > 0 else None,
            "base_price_cr": round(float(row.get("base_price_cr") or 0), 2) or None,
            "num_bids": int(row.get("num_bids") or 0),
            "bid_won": bool(row.get("bid_won")),
            "bubble_price_cr": bubble,
            "has_stats": stats is not None,
            "has_bid_data": bid_cr > 0 or int(row.get("num_bids") or 0) > 0,
            "facecard_url": facecards.get(_normalize_key(name)),
        }
        if filt == "inform" and (rec.get("form_rating") or 0) <= 60:
            continue
        out.append(rec)

    if filt == "batters":
        out.sort(key=lambda p: p.get("total_runs") or 0, reverse=True)
    elif filt == "bowlers":
        out.sort(key=lambda p: p.get("total_wickets") or 0, reverse=True)
    elif filt == "allrounders":
        out.sort(
            key=lambda p: (p.get("total_runs") or 0) * 0.5 + (p.get("total_wickets") or 0) * 5,
            reverse=True,
        )
    elif filt == "inform":
        out.sort(key=lambda p: p.get("form_rating") or 0, reverse=True)
    elif filt == "all":
        out.sort(key=lambda p: p.get("bubble_price_cr") or 0, reverse=True)

    return out[:limit]


def auction_pool_meta(conn: sqlite3.Connection, year: int = 2026) -> Dict[str, Any]:
    yr = _pool_frame(conn, year)
    bids = _load_bid_aggregate(year)
    return {
        "year": year,
        "pool_size": int(len(yr)),
        "bid_history_size": int(len(bids)),
        "available": not yr.empty,
    }
