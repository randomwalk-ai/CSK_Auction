#!/usr/bin/env python3
"""
CSK Auction Intelligence — bid-war analysis from Cricbuzz all-bids scrape.

Turns franchise bid sheets into actionable auction room insights:
  - Squad gaps inferred from who CSK chased (won + lost)
  - Lost battles: who beat CSK, by how much, league demand
  - Win-rate / spend patterns by role, country, price band
  - Head-to-head rivalry vs other franchises
  - Year-over-year targeting strategy shifts
  - Overall career analysis + inferred auction philosophy (with --overall)

Usage:
  python scripts/analyze_csk_auction_intelligence.py              # single year (2026)
  python scripts/analyze_csk_auction_intelligence.py --overall  # 2018–2026 career view
  python scripts/analyze_csk_auction_intelligence.py --year 2026 --team CSK
  python scripts/analyze_csk_auction_intelligence.py --overall --from-year 2018 --to-year 2026
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = ROOT / "data" / "cricbuzz_all_bids" / "all_bids_master.csv"
DEFAULT_DB = ROOT / "auction_data.db"
REPORT_DIR = ROOT / "reports" / "csk_intelligence"

# Mirror api/app.py SquadConfig ideal composition
IDEAL_ROLE = {
    "Batter": 6,
    "Bowler": 6,
    "All Rounder": 5,
    "Wicket Keeper": 2,
}
MAX_OVERSEAS = 8
MIN_INDIAN = 10


@dataclass
class AuctionIntel:
    team: str
    year: int
    summary: Dict[str, Any] = field(default_factory=dict)
    squad_gap_signals: List[Dict[str, Any]] = field(default_factory=list)
    won_targets: List[Dict[str, Any]] = field(default_factory=list)
    lost_targets: List[Dict[str, Any]] = field(default_factory=list)
    high_demand_misses: List[Dict[str, Any]] = field(default_factory=list)
    rival_head_to_head: List[Dict[str, Any]] = field(default_factory=list)
    role_win_rates: List[Dict[str, Any]] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)
    historical_trends: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OverallIntel:
    team: str
    from_year: int
    to_year: int
    summary: Dict[str, Any] = field(default_factory=dict)
    strategy_profile: Dict[str, Any] = field(default_factory=dict)
    era_playbooks: List[Dict[str, Any]] = field(default_factory=list)
    price_band_analysis: List[Dict[str, Any]] = field(default_factory=list)
    role_win_rates: List[Dict[str, Any]] = field(default_factory=list)
    recurring_misses: List[Dict[str, Any]] = field(default_factory=list)
    career_rivals: List[Dict[str, Any]] = field(default_factory=list)
    what_works: List[str] = field(default_factory=list)
    what_fails: List[str] = field(default_factory=list)
    strategy_narrative: List[str] = field(default_factory=list)
    forward_playbook: List[str] = field(default_factory=list)
    yearly_trends: List[Dict[str, Any]] = field(default_factory=list)


def normalize_role(role: str) -> str:
    r = (role or "").strip()
    if r in ("WK-Batter", "WK Batter", "Wicket Keeper", "Wicketkeeper"):
        return "Wicket Keeper"
    if r in ("Allrounder", "All-Rounder", "All Rounder"):
        return "All Rounder"
    if r in ("Batter", "Batsman", "Batting"):
        return "Batter"
    if r in ("Bowler", "Bowling"):
        return "Bowler"
    return r or "Unknown"


def is_overseas(country: str) -> bool:
    c = (country or "").strip().lower()
    return c not in ("", "india", "indian")


def load_bids(csv_path: Path, db_path: Path, from_year: int, to_year: int) -> pd.DataFrame:
    if csv_path.exists():
        df = pd.read_csv(csv_path)
    elif db_path.exists():
        conn = sqlite3.connect(db_path)
        df = pd.read_sql(
            """
            SELECT year, viewing_team_code, player_name, role, country,
                   num_bids, last_bid_cr, bid_war, source
            FROM bid_history
            WHERE source = 'cricbuzz_all_bids_playwright'
            """,
            conn,
        )
        conn.close()
    else:
        raise FileNotFoundError(f"No bid data at {csv_path} or {db_path}")

    df = df[df["year"].between(from_year, to_year)].copy()
    df["viewing_team_code"] = df["viewing_team_code"].fillna("").astype(str).str.strip().str.upper()
    df["player_name"] = df["player_name"].astype(str).str.strip()
    df["role_bucket"] = df["role"].map(normalize_role)
    df["overseas"] = df["country"].map(is_overseas)
    df["num_bids"] = pd.to_numeric(df.get("num_bids", df.get("bids", 0)), errors="coerce").fillna(0).astype(int)
    df["last_bid_cr"] = pd.to_numeric(df.get("last_bid_cr", 0), errors="coerce").fillna(0.0)
    df["bid_war"] = df["bid_war"].fillna("").astype(str).str.strip().str.lower()
    df["won"] = df["bid_war"] == "won"
    return df


def league_player_demand(df: pd.DataFrame, year: int) -> pd.DataFrame:
    """Per player: how many franchises bid, total bids, winning team, final price."""
    yr = df[df["year"] == year].copy()
    if yr.empty:
        return pd.DataFrame()

    agg = yr.groupby("player_name", as_index=False).agg(
        franchises_bidding=("viewing_team_code", "nunique"),
        total_bids=("num_bids", "sum"),
        max_bids_single_team=("num_bids", "max"),
        final_price_cr=("last_bid_cr", "max"),
        role=("role_bucket", "first"),
        country=("country", "first"),
    )
    winners = yr[yr["won"]][["player_name", "viewing_team_code", "last_bid_cr"]].rename(
        columns={"viewing_team_code": "winner_team", "last_bid_cr": "winner_price_cr"}
    )
    # If duplicate winners (data glitch), keep highest price row
    winners = winners.sort_values("winner_price_cr", ascending=False).drop_duplicates("player_name")
    return agg.merge(winners, on="player_name", how="left")


def infer_squad_gaps(team_targets: pd.DataFrame, team_won: pd.DataFrame) -> List[Dict[str, Any]]:
    """Gaps = roles CSK chased heavily but still under ideal after wins."""
    signals: List[Dict[str, Any]] = []

    target_roles = team_targets["role_bucket"].value_counts().to_dict()
    won_roles = team_won["role_bucket"].value_counts().to_dict()
    target_overseas = int(team_targets["overseas"].sum())
    won_overseas = int(team_won["overseas"].sum())

    for role, ideal in IDEAL_ROLE.items():
        chased = target_roles.get(role, 0)
        acquired = won_roles.get(role, 0)
        gap_after = ideal - acquired
        if chased >= 2 and acquired < ideal:
            intensity = "Critical" if chased >= 3 and acquired <= 1 else "High" if chased >= 2 else "Medium"
            signals.append(
                {
                    "signal": "role_gap",
                    "role": role,
                    "ideal": ideal,
                    "targets_chased": chased,
                    "won": acquired,
                    "still_need": max(0, gap_after),
                    "priority": intensity,
                    "insight": (
                        f"CSK entered bid wars for {chased} {role}(s) but only won {acquired}. "
                        f"Ideal squad needs {ideal}; still short by ~{max(0, gap_after)}."
                    ),
                }
            )

    if target_overseas >= 4 and won_overseas < MAX_OVERSEAS:
        signals.append(
            {
                "signal": "overseas_pivot",
                "targets_chased": target_overseas,
                "won_overseas": won_overseas,
                "priority": "High",
                "insight": (
                    f"CSK chased {target_overseas} overseas players (won {won_overseas}). "
                    "Suggests active overseas upgrade / backup planning."
                ),
            }
        )

    # Death bowling / premium allrounders heuristic from high-price lost targets
    premium_lost = team_targets[(~team_targets["won"]) & (team_targets["last_bid_cr"] >= 5.0)]
    death_bowlers = premium_lost[premium_lost["role_bucket"] == "Bowler"]
    allrounders = premium_lost[premium_lost["role_bucket"] == "All Rounder"]
    if len(death_bowlers) >= 2:
        names = ", ".join(death_bowlers["player_name"].head(3).tolist())
        signals.append(
            {
                "signal": "premium_bowling_miss",
                "priority": "Critical",
                "players": death_bowlers["player_name"].tolist(),
                "insight": f"Lost expensive bowling targets ({names}) — likely death / strike bowling gap.",
            }
        )
    if len(allrounders) >= 2:
        names = ", ".join(allrounders["player_name"].head(3).tolist())
        signals.append(
            {
                "signal": "premium_allrounder_miss",
                "priority": "High",
                "players": allrounders["player_name"].tolist(),
                "insight": f"Lost multiple premium allrounders ({names}) — balance / flexibility gap.",
            }
        )

    return sorted(signals, key=lambda x: {"Critical": 0, "High": 1, "Medium": 2}.get(x.get("priority", "Medium"), 3))


def lost_battle_details(
    csk_lost: pd.DataFrame, league: pd.DataFrame, all_teams_yr: pd.DataFrame
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for _, r in csk_lost.sort_values(["last_bid_cr", "num_bids"], ascending=False).iterrows():
        name = r["player_name"]
        league_row = league[league["player_name"] == name]
        winner = league_row["winner_team"].iloc[0] if len(league_row) else None
        winner_price = float(league_row["winner_price_cr"].iloc[0]) if len(league_row) else None
        franchises = int(league_row["franchises_bidding"].iloc[0]) if len(league_row) else None
        total_bids = int(league_row["total_bids"].iloc[0]) if len(league_row) else None

        # Other teams that also lost (competition for CSK's last bid)
        competitors = all_teams_yr[
            (all_teams_yr["player_name"] == name)
            & (all_teams_yr["viewing_team_code"] != "CSK")
            & (~all_teams_yr["won"])
        ]["viewing_team_code"].tolist()

        premium = r["last_bid_cr"] >= 5.0 or r["num_bids"] >= 10
        why = []
        if r["num_bids"] >= 15:
            why.append(f"Fierce bid war ({int(r['num_bids'])} CSK bids)")
        elif r["num_bids"] >= 5:
            why.append(f"Moderate competition ({int(r['num_bids'])} CSK bids)")
        if franchises and franchises >= 4:
            why.append(f"{franchises} franchises in the fight")
        if winner and winner_price and winner_price > r["last_bid_cr"]:
            delta = round(winner_price - r["last_bid_cr"], 2)
            why.append(f"{winner} paid {winner_price:.2f} Cr (+{delta:.2f} vs CSK exit)")
        if r["role_bucket"] in ("Bowler", "All Rounder") and r["last_bid_cr"] >= 5:
            why.append(f"Premium {r['role_bucket'].lower()} — core squad need")
        if r["overseas"]:
            why.append("Overseas slot / impact player hunt")

        rows.append(
            {
                "player": name,
                "role": r["role_bucket"],
                "country": r["country"],
                "csk_last_bid_cr": round(float(r["last_bid_cr"]), 2),
                "csk_bids": int(r["num_bids"]),
                "winner": winner,
                "winner_price_cr": round(winner_price, 2) if winner_price else None,
                "franchises_bidding": franchises,
                "league_total_bids": total_bids,
                "other_losing_bidders": competitors[:5],
                "demand_tier": "Premium" if premium else "Standard",
                "why_csk_wanted": "; ".join(why) if why else "Depth / squad fill",
            }
        )
    return rows


def rival_head_to_head(csk_yr: pd.DataFrame, all_teams_yr: pd.DataFrame) -> List[Dict[str, Any]]:
    """When CSK lost, which franchise won the same player most often."""
    lost_players = set(csk_yr[~csk_yr["won"]]["player_name"])
    if not lost_players:
        return []

    counter: Counter = Counter()
    details: Dict[str, List[str]] = defaultdict(list)
    for player in lost_players:
        winners = all_teams_yr[(all_teams_yr["player_name"] == player) & (all_teams_yr["won"])]
        for _, w in winners.iterrows():
            rival = w["viewing_team_code"]
            if rival and rival != "CSK":
                counter[rival] += 1
                details[rival].append(f"{player} ({w['last_bid_cr']:.2f} Cr)")

    return [
        {
            "rival": rival,
            "players_won_vs_csk": count,
            "examples": details[rival][:4],
        }
        for rival, count in counter.most_common(8)
    ]


def role_win_rates(csk_yr: pd.DataFrame) -> List[Dict[str, Any]]:
    out = []
    for role, grp in csk_yr.groupby("role_bucket"):
        wins = int(grp["won"].sum())
        total = len(grp)
        out.append(
            {
                "role": role,
                "targets": total,
                "won": wins,
                "lost": total - wins,
                "win_rate_pct": round(100 * wins / total, 1) if total else 0,
                "avg_price_won_cr": round(grp[grp["won"]]["last_bid_cr"].mean(), 2) if wins else 0,
                "avg_price_lost_cr": round(grp[~grp["won"]]["last_bid_cr"].mean(), 2) if total - wins else 0,
            }
        )
    return sorted(out, key=lambda x: -x["targets"])


def high_demand_misses(csk_lost: pd.DataFrame, league: pd.DataFrame) -> List[Dict[str, Any]]:
    merged = csk_lost.merge(league, on="player_name", how="left", suffixes=("_csk", "_lg"))
    role_col = "role_bucket_csk" if "role_bucket_csk" in merged.columns else "role_bucket"
    merged = merged.sort_values(["total_bids", "last_bid_cr"], ascending=False)
    rows = []
    for _, r in merged.head(12).iterrows():
        rows.append(
            {
                "player": r["player_name"],
                "role": r[role_col],
                "csk_exit_cr": round(float(r["last_bid_cr"]), 2),
                "winner": r.get("winner_team"),
                "winning_price_cr": round(float(r["winner_price_cr"]), 2) if pd.notna(r.get("winner_price_cr")) else None,
                "league_total_bids": int(r["total_bids"]) if pd.notna(r.get("total_bids")) else None,
                "franchises": int(r["franchises_bidding"]) if pd.notna(r.get("franchises_bidding")) else None,
            }
        )
    return rows


def build_recommendations(intel: AuctionIntel) -> List[str]:
    recs: List[str] = []
    s = intel.summary

    if s.get("win_rate_pct", 100) < 55:
        recs.append(
            f"Win rate {s['win_rate_pct']}% on bid targets — tighten purse on low-priority names; "
            "concentrate firepower on 2–3 marquee gaps."
        )

    for gap in intel.squad_gap_signals:
        if gap.get("priority") == "Critical":
            recs.append(gap["insight"] + " → Pre-set walk-away price + backup list at 30% lower band.")

    for rival in intel.rival_head_to_head[:3]:
        recs.append(
            f"Head-to-head: {rival['rival']} beat CSK on {rival['players_won_vs_csk']} shared targets "
            f"({', '.join(rival['examples'][:2])}). Scout their remaining purse timing."
        )

    for role in intel.role_win_rates:
        if role["targets"] >= 3 and role["win_rate_pct"] < 40:
            recs.append(
                f"{role['role']}: only {role['win_rate_pct']}% win rate on {role['targets']} chases "
                f"(lost avg {role['avg_price_lost_cr']} Cr). Consider early-anchor strategy or alternate profile."
            )

    for miss in intel.high_demand_misses[:3]:
        if miss.get("league_total_bids", 0) and miss["league_total_bids"] >= 20:
            recs.append(
                f"{miss['player']}: league-wide heat ({miss['league_total_bids']} total bids). "
                "Either commit ceiling early or pivot to same-role Plan B before auction day."
            )

    if s.get("spend_won_cr", 0) > 0 and s.get("avg_won_price_cr", 0) > 4:
        recs.append(
            f"Average won price {s['avg_won_price_cr']:.2f} Cr — heavy top-heavy spend. "
            "Reserve ₹15–20 Cr for late uncapped / RTM-style value picks."
        )

    if not recs:
        recs.append("Balanced auction execution — maintain role-based walk-away sheets for next window.")

    return recs[:10]


def historical_trends(csk_hist: pd.DataFrame) -> Dict[str, Any]:
    yearly = []
    for year, grp in csk_hist.groupby("year"):
        wins = int(grp["won"].sum())
        total = len(grp)
        yearly.append(
            {
                "year": int(year),
                "targets": total,
                "won": wins,
                "win_rate_pct": round(100 * wins / total, 1) if total else 0,
                "spend_won_cr": round(grp[grp["won"]]["last_bid_cr"].sum(), 2),
                "top_role_chased": grp["role_bucket"].value_counts().index[0] if total else None,
            }
        )
    yearly.sort(key=lambda x: x["year"])

    role_hist = (
        csk_hist.groupby("role_bucket")
        .agg(targets=("player_name", "count"), wins=("won", "sum"))
        .reset_index()
    )
    role_hist["win_rate_pct"] = (100 * role_hist["wins"] / role_hist["targets"]).round(1)

    return {
        "by_year": yearly,
        "role_career": role_hist.to_dict(orient="records"),
        "most_chased_players": (
            csk_hist.groupby("player_name")
            .size()
            .sort_values(ascending=False)
            .head(5)
            .reset_index(name="times_targeted")
            .to_dict(orient="records")
        ),
    }


PRICE_BANDS = [
    ("Value", 0, 1.0),
    ("Mid", 1.0, 5.0),
    ("Premium", 5.0, 10.0),
    ("Marquee", 10.0, 999.0),
]

ERA_RANGES = [
    ("2018 mega auction", 2018, 2018),
    ("2019–2021 retention era", 2019, 2021),
    ("2022 mega rebuild", 2022, 2022),
    ("2023–2024 surgical window", 2023, 2024),
    ("2025–2026 core + overseas hunt", 2025, 2026),
]


def _price_band(price: float) -> str:
    for name, lo, hi in PRICE_BANDS:
        if lo <= price < hi:
            return name
    return "Marquee"


def career_rival_analysis(csk: pd.DataFrame, df: pd.DataFrame) -> List[Dict[str, Any]]:
    lost = csk[~csk["won"]]
    counter: Counter = Counter()
    examples: Dict[str, List[str]] = defaultdict(list)
    for _, row in lost.iterrows():
        year = row["year"]
        player = row["player_name"]
        winners = df[
            (df["year"] == year)
            & (df["player_name"] == player)
            & (df["won"])
            & (df["viewing_team_code"] != csk["viewing_team_code"].iloc[0])
        ]
        for _, w in winners.iterrows():
            rival = w["viewing_team_code"]
            counter[rival] += 1
            if len(examples[rival]) < 6:
                examples[rival].append(f"{player} ({int(year)}, {w['last_bid_cr']:.2f} Cr)")
    return [
        {"rival": r, "career_wins_vs_team": c, "notable_wins": examples[r][:5]}
        for r, c in counter.most_common(10)
    ]


def recurring_miss_profiles(csk: pd.DataFrame, df: pd.DataFrame) -> List[Dict[str, Any]]:
    """Premium losses CSK keeps repeating — grouped by role + overseas + price."""
    lost = csk[~csk["won"]].copy()
    lost["price_band"] = lost["last_bid_cr"].map(_price_band)
    lost_premium = lost[lost["last_bid_cr"] >= 3.0].sort_values("last_bid_cr", ascending=False)

    rows: List[Dict[str, Any]] = []
    for _, r in lost_premium.head(20).iterrows():
        year = int(r["year"])
        player = r["player_name"]
        yr_df = df[df["year"] == year]
        winner_row = yr_df[(yr_df["player_name"] == player) & (yr_df["won"])]
        winner = winner_row["viewing_team_code"].iloc[0] if len(winner_row) else None
        winner_price = float(winner_row["last_bid_cr"].iloc[0]) if len(winner_row) else None
        delta = round(winner_price - r["last_bid_cr"], 2) if winner_price else None
        rows.append(
            {
                "year": year,
                "player": player,
                "role": r["role_bucket"],
                "overseas": bool(r["overseas"]),
                "csk_exit_cr": round(float(r["last_bid_cr"]), 2),
                "csk_bids": int(r["num_bids"]),
                "winner": winner,
                "winner_price_cr": round(winner_price, 2) if winner_price else None,
                "miss_margin_cr": delta,
                "profile": f"{r['role_bucket']} · {'Overseas' if r['overseas'] else 'Indian'} · {_price_band(r['last_bid_cr'])}",
            }
        )
    return rows


def infer_era_playbook(label: str, era_df: pd.DataFrame) -> Dict[str, Any]:
    if era_df.empty:
        return {"era": label, "note": "No data"}

    won = era_df[era_df["won"]]
    top_role = era_df["role_bucket"].value_counts().index[0]
    win_rate = round(100 * len(won) / len(era_df), 1)
    spend = round(won["last_bid_cr"].sum(), 2)
    overseas_pct = round(100 * era_df["overseas"].mean(), 1)
    snipe_wins = int((won["num_bids"] <= 1).sum())
    bid_wars_lost = int(((~era_df["won"]) & (era_df["num_bids"] >= 10)).sum())

    strategy = []
    if len(era_df) >= 35:
        strategy.append("Wide net — entered many bid sheets (mega-auction / full rebuild mode).")
    elif len(era_df) <= 10:
        strategy.append("Minimal table — mostly retentions / RTM; only filled specific holes.")
    if top_role == "Bowler":
        strategy.append("Bowling-first shopping list — depth and strike options prioritized.")
    elif top_role == "All Rounder":
        strategy.append("Flexibility-first — chased multi-role profiles for balance.")
    if overseas_pct >= 45:
        strategy.append(f"Heavy overseas focus ({overseas_pct}% of targets).")
    if snipe_wins >= len(won) * 0.4 and len(won) > 0:
        strategy.append(f"Value sniper — {snipe_wins}/{len(won)} wins at ≤1 bid (no fight).")
    if bid_wars_lost >= 3:
        strategy.append(f"Lost {bid_wars_lost} bid wars (10+ bids) — outgunned on marquee names.")

    return {
        "era": label,
        "years": f"{int(era_df['year'].min())}–{int(era_df['year'].max())}",
        "targets": len(era_df),
        "won": len(won),
        "win_rate_pct": win_rate,
        "spend_won_cr": spend,
        "top_role_chased": top_role,
        "overseas_target_pct": overseas_pct,
        "inferred_strategy": strategy,
    }


def infer_strategy_profile(csk: pd.DataFrame, df: pd.DataFrame) -> Dict[str, Any]:
    won = csk[csk["won"]]
    lost = csk[~csk["won"]]

    snipe_wins = int((won["num_bids"] <= 1).sum())
    war_losses = lost[lost["num_bids"] >= 10]
    close_losses = []
    for _, r in lost.iterrows():
        yr = df[(df["year"] == r["year"]) & (df["player_name"] == r["player_name"]) & (df["won"])]
        if len(yr):
            delta = float(yr["last_bid_cr"].iloc[0]) - float(r["last_bid_cr"])
            if 0 < delta <= 0.5:
                close_losses.append(r["player_name"])

    premium = csk[csk["last_bid_cr"] >= 5.0]
    premium_won = premium[premium["won"]]
    overseas_premium_lost = lost[(lost["overseas"]) & (lost["last_bid_cr"] >= 5.0)]

    role_chase = csk["role_bucket"].value_counts(normalize=True).mul(100).round(1).to_dict()
    top_chased_role = csk["role_bucket"].value_counts().index[0]

    return {
        "archetype": _infer_archetype(csk, won, lost),
        "career_win_rate_pct": round(100 * len(won) / len(csk), 1) if len(csk) else 0,
        "total_spend_won_cr": round(won["last_bid_cr"].sum(), 2),
        "avg_targets_per_year": round(len(csk) / csk["year"].nunique(), 1),
        "top_chased_role": top_chased_role,
        "role_mix_pct": role_chase,
        "snipe_win_count": snipe_wins,
        "snipe_win_pct_of_wins": round(100 * snipe_wins / len(won), 1) if len(won) else 0,
        "bid_wars_lost": len(war_losses),
        "close_losses_under_50L": len(close_losses),
        "close_loss_examples": close_losses[:8],
        "premium_chase_win_rate_pct": round(100 * len(premium_won) / len(premium), 1) if len(premium) else 0,
        "overseas_premium_losses": len(overseas_premium_lost),
        "overseas_chase_win_rate_pct": round(
            100 * csk[csk["overseas"] & csk["won"]].shape[0] / max(1, csk["overseas"].sum()), 1
        ),
        "indian_chase_win_rate_pct": round(
            100 * csk[(~csk["overseas"]) & csk["won"]].shape[0] / max(1, (~csk["overseas"]).sum()), 1
        ),
    }


def _infer_archetype(csk: pd.DataFrame, won: pd.DataFrame, lost: pd.DataFrame) -> str:
    bowler_share = (csk["role_bucket"] == "Bowler").mean()
    snipe_rate = (won["num_bids"] <= 1).mean() if len(won) else 0
    premium_loss_rate = lost[lost["last_bid_cr"] >= 5.0].shape[0] / max(1, len(lost))

    if bowler_share >= 0.35 and snipe_rate >= 0.35:
        return "Bowling-depth value builder"
    if premium_loss_rate >= 0.35:
        return "Marquee chaser with selective execution"
    if (csk["role_bucket"] == "All Rounder").mean() >= 0.35:
        return "Flex-first squad architect"
    return "Balanced squad filler"


def price_band_analysis(csk: pd.DataFrame) -> List[Dict[str, Any]]:
    rows = []
    for name, lo, hi in PRICE_BANDS:
        band = csk[(csk["last_bid_cr"] >= lo) & (csk["last_bid_cr"] < hi)]
        if band.empty:
            continue
        wins = int(band["won"].sum())
        rows.append(
            {
                "band": name,
                "range_cr": f"₹{lo}–{hi if hi < 100 else '∞'}",
                "targets": len(band),
                "won": wins,
                "win_rate_pct": round(100 * wins / len(band), 1),
                "spend_won_cr": round(band[band["won"]]["last_bid_cr"].sum(), 2),
            }
        )
    return rows


def build_strategy_narrative(intel: OverallIntel) -> List[str]:
    sp = intel.strategy_profile
    lines = []

    lines.append(
        f"**Identified archetype: {sp.get('archetype', 'Unknown')}** — "
        f"over {intel.from_year}–{intel.to_year}, CSK entered {intel.summary.get('targets', 0)} bid sheets, "
        f"winning {intel.summary.get('win_rate_pct', 0)}% and spending ₹{sp.get('total_spend_won_cr', 0):.2f} Cr on acquisitions."
    )

    top_role = sp.get("top_chased_role", "Bowler")
    mix = sp.get("role_mix_pct", {})
    lines.append(
        f"**Primary shopping lens: {top_role}** ({mix.get(top_role, 0)}% of all targets). "
        "CSK's bid history reads as a franchise that builds around bowling depth first, "
        "then adds allround flexibility — batters are targeted selectively, often only when a marquee overseas slot opens."
    )

    snipe = sp.get("snipe_win_pct_of_wins", 0)
    if snipe >= 30:
        lines.append(
            f"**Value-sniping is core to CSK's wins** — {snipe}% of successful bids needed ≤1 raise "
            f"({sp.get('snipe_win_count', 0)} players). Strategy: identify uncapped / undervalued names early, "
            "don't engage in wars unless the player fills a declared gap."
        )

    close = sp.get("close_losses_under_50L", 0)
    if close >= 5:
        examples = ", ".join(sp.get("close_loss_examples", [])[:4])
        lines.append(
            f"**Marquee misses are often margin calls, not budget failures** — {close} losses where the winner "
            f"paid ≤₹0.50 Cr more ({examples}). CSK likely had the right target but exited one increment early — "
            "auction-room hesitation on premium names, not wrong profiling."
        )

    prem_wr = sp.get("premium_chase_win_rate_pct", 0)
    if prem_wr < 45:
        lines.append(
            f"**Premium band (≥₹5 Cr) is CSK's weakest zone** — only {prem_wr}% win rate. "
            "When CSK chases expensive overseas batters or strike bowlers, multiple franchises pile in. "
            "Historical pattern: commit a hard ceiling pre-auction or pivot to Plan B in the same role at ₹3–4 Cr."
        )

    o_wr = sp.get("overseas_chase_win_rate_pct", 0)
    i_wr = sp.get("indian_chase_win_rate_pct", 0)
    if i_wr > o_wr + 10:
        lines.append(
            f"**Indian targets convert better** ({i_wr}% win vs {o_wr}% overseas). "
            "2026 fits this: heavy spend on Kartik Sharma / Prashant Veer (Indian core), "
            "while overseas marquees (Green, Mustafizur, Holder) slipped. "
            "Likely strategy: anchor squad with Indian spine via auction, use overseas cap on 2–3 difference-makers."
        )

    for era in intel.era_playbooks:
        if era.get("inferred_strategy"):
            lines.append(
                f"**{era['era']} ({era.get('years', '')})**: "
                + " ".join(era["inferred_strategy"][:2])
            )

    return lines


def build_forward_playbook(intel: OverallIntel) -> List[str]:
    recs: List[str] = []
    sp = intel.strategy_profile

    recs.append(
        "Run a two-tier purse model: Tier A (2 slots, ₹20+ Cr combined ceiling) for declared gaps only; "
        "Tier B (remaining purse) for snipe targets at ≤₹2 Cr — this mirrors CSK's highest win-rate band."
    )

    for band in intel.price_band_analysis:
        if band["band"] == "Value" and band["win_rate_pct"] >= 50:
            recs.append(
                f"Double down on Value band (≤₹1 Cr): {band['win_rate_pct']}% career win rate on {band['targets']} targets — "
                "assign a dedicated scout list of 15 names before the auction."
            )
        if band["band"] == "Marquee" and band["win_rate_pct"] < 40:
            recs.append(
                f"Marquee band (≥₹10 Cr): only {band['win_rate_pct']}% win rate — enter with a non-negotiable walk-away; "
                "if 3+ franchises bid within first ₹5 Cr, pivot immediately."
            )

    top_rival = intel.career_rivals[0] if intel.career_rivals else None
    if top_rival:
        recs.append(
            f"Track {top_rival['rival']} purse in real time — career leader in beating CSK "
            f"({top_rival['career_wins_vs_team']} wins on shared targets)."
        )

    for role in intel.role_win_rates:
        if role["targets"] >= 20 and role["win_rate_pct"] < 45:
            recs.append(
                f"{role['role']}: {role['win_rate_pct']}% career win rate despite {role['targets']} chases — "
                f"stop entering late; either lead the bid or pre-identify alternate profile."
            )

    if sp.get("overseas_premium_losses", 0) >= 5:
        recs.append(
            "Overseas premium plan: pick max 2 overseas marquee targets pre-auction with absolute ceilings; "
            "maintain 3 same-role backups at 40–50% of that price — CSK loses overseas wars when improvising."
        )

    recs.append(
        "Post-auction review metric: track 'close loss' count (≤₹0.5 Cr gap) — if >3 in a window, "
        "train auctioneer on increment discipline for Tier A names."
    )

    return recs[:12]


def analyze_team_overall(df: pd.DataFrame, team: str, from_year: int, to_year: int) -> OverallIntel:
    team = team.upper()
    csk = df[(df["viewing_team_code"] == team)].copy()
    intel = OverallIntel(team=team, from_year=from_year, to_year=to_year)

    if csk.empty:
        intel.strategy_narrative = [f"No bid data for {team} between {from_year} and {to_year}."]
        return intel

    won = csk[csk["won"]]
    lost = csk[~csk["won"]]

    intel.summary = {
        "targets": len(csk),
        "won": len(won),
        "lost": len(lost),
        "win_rate_pct": round(100 * len(won) / len(csk), 1),
        "spend_won_cr": round(won["last_bid_cr"].sum(), 2),
        "avg_won_price_cr": round(won["last_bid_cr"].mean(), 2) if len(won) else 0,
        "avg_lost_exit_cr": round(lost["last_bid_cr"].mean(), 2) if len(lost) else 0,
        "overseas_chased": int(csk["overseas"].sum()),
        "overseas_won": int(won["overseas"].sum()),
        "years_active": int(csk["year"].nunique()),
        "roles_chased": csk["role_bucket"].value_counts().to_dict(),
        "roles_won": won["role_bucket"].value_counts().to_dict(),
    }

    intel.strategy_profile = infer_strategy_profile(csk, df)
    intel.era_playbooks = [
        infer_era_playbook(label, csk[csk["year"].between(y0, y1)])
        for label, y0, y1 in ERA_RANGES
        if not csk[csk["year"].between(y0, y1)].empty
    ]
    intel.price_band_analysis = price_band_analysis(csk)
    intel.role_win_rates = role_win_rates(csk)
    intel.recurring_misses = recurring_miss_profiles(csk, df)
    intel.career_rivals = career_rival_analysis(csk, df)
    intel.yearly_trends = historical_trends(csk)["by_year"]

    # What works / fails
    for band in intel.price_band_analysis:
        if band["win_rate_pct"] >= 55 and band["targets"] >= 10:
            intel.what_works.append(
                f"{band['band']} band ({band['range_cr']}): {band['win_rate_pct']}% win rate, "
                f"₹{band['spend_won_cr']:.2f} Cr acquired"
            )
    for role in intel.role_win_rates:
        if role["targets"] >= 15 and role["win_rate_pct"] >= 50:
            intel.what_works.append(
                f"{role['role']}: {role['win_rate_pct']}% win rate on {role['targets']} career targets"
            )
        if role["targets"] >= 15 and role["win_rate_pct"] < 42:
            intel.what_fails.append(
                f"{role['role']}: only {role['win_rate_pct']}% win rate despite {role['targets']} chases "
                f"(avg lost exit ₹{role['avg_price_lost_cr']} Cr)"
            )

    if sp_snipe := intel.strategy_profile.get("snipe_win_pct_of_wins", 0):
        if sp_snipe >= 30:
            intel.what_works.append(f"Single-bid snipes: {sp_snipe}% of all CSK wins")

    if intel.strategy_profile.get("premium_chase_win_rate_pct", 100) < 45:
        intel.what_fails.append(
            f"Premium chases (≥₹5 Cr): {intel.strategy_profile['premium_chase_win_rate_pct']}% win rate"
        )

    intel.strategy_narrative = build_strategy_narrative(intel)
    intel.forward_playbook = build_forward_playbook(intel)
    return intel


def render_overall_markdown(intel: OverallIntel) -> str:
    s = intel.summary
    sp = intel.strategy_profile
    lines = [
        f"# {intel.team} Overall Auction Intelligence — IPL {intel.from_year}–{intel.to_year}",
        "",
        "## Career executive snapshot",
        "",
        f"- **Bid targets:** {s.get('targets', 0)} across {s.get('years_active', 0)} auctions",
        f"- **Won / Lost:** {s.get('won', 0)} / {s.get('lost', 0)} | **Career win rate:** {s.get('win_rate_pct', 0)}%",
        f"- **Total spend on wins:** ₹{s.get('spend_won_cr', 0):.2f} Cr | **Avg won price:** ₹{s.get('avg_won_price_cr', 0):.2f} Cr",
        f"- **Overseas chased/won:** {s.get('overseas_chased', 0)}/{s.get('overseas_won', 0)}",
        f"- **Archetype:** {sp.get('archetype', '—')}",
        "",
        "## Inferred auction strategy (what CSK was likely doing)",
        "",
    ]
    for para in intel.strategy_narrative:
        lines.append(f"- {para}")

    lines.extend(["", "## Era-by-era playbook", ""])
    for era in intel.era_playbooks:
        lines.append(f"### {era['era']} ({era.get('years', '')})")
        lines.append(
            f"Targets: {era.get('targets')} | Won: {era.get('won')} | Win rate: {era.get('win_rate_pct')}% | "
            f"Spend: ₹{era.get('spend_won_cr', 0):.2f} Cr | Top role: {era.get('top_role_chased')}"
        )
        for note in era.get("inferred_strategy", []):
            lines.append(f"- {note}")
        lines.append("")

    lines.extend(["## What consistently works", ""])
    for w in intel.what_works or ["No dominant winning pattern detected."]:
        lines.append(f"- {w}")

    lines.extend(["", "## What repeatedly fails", ""])
    for f in intel.what_fails or ["No strong failure pattern detected."]:
        lines.append(f"- {f}")

    lines.extend(["", "## Price band performance", ""])
    lines.append("| Band | Range | Targets | Won | Win % | Spend |")
    lines.append("|------|-------|---------|-----|-------|-------|")
    for b in intel.price_band_analysis:
        lines.append(
            f"| {b['band']} | {b['range_cr']} | {b['targets']} | {b['won']} | {b['win_rate_pct']}% | ₹{b['spend_won_cr']:.2f} Cr |"
        )

    lines.extend(["", "## Role win rates (career)", ""])
    lines.append("| Role | Targets | Won | Win % | Avg won | Avg lost exit |")
    lines.append("|------|---------|-----|-------|---------|---------------|")
    for r in intel.role_win_rates:
        lines.append(
            f"| {r['role']} | {r['targets']} | {r['won']} | {r['win_rate_pct']}% | "
            f"{r['avg_price_won_cr']} Cr | {r['avg_price_lost_cr']} Cr |"
        )

    lines.extend(["", "## Career franchise rivals", ""])
    for rival in intel.career_rivals[:8]:
        lines.append(
            f"- **{rival['rival']}** — {rival['career_wins_vs_team']} wins vs {intel.team}: "
            f"{'; '.join(rival['notable_wins'][:3])}"
        )

    lines.extend(["", "## Recurring premium misses", ""])
    lines.append("| Year | Player | Profile | CSK exit | Winner | Gap |")
    lines.append("|------|--------|---------|----------|--------|-----|")
    for m in intel.recurring_misses[:15]:
        gap = f"+₹{m['miss_margin_cr']:.2f} Cr" if m.get("miss_margin_cr") is not None else "—"
        lines.append(
            f"| {m['year']} | {m['player']} | {m['profile']} | ₹{m['csk_exit_cr']:.2f} Cr | "
            f"{m.get('winner') or '—'} | {gap} |"
        )

    lines.extend(["", "## Year-by-year trends", ""])
    lines.append("| Year | Targets | Won | Win % | Spend | Top role chased |")
    lines.append("|------|---------|-----|-------|-------|-----------------|")
    for y in intel.yearly_trends:
        lines.append(
            f"| {y['year']} | {y['targets']} | {y['won']} | {y['win_rate_pct']}% | "
            f"₹{y['spend_won_cr']:.2f} Cr | {y['top_role_chased']} |"
        )

    lines.extend(["", "## Forward playbook (next auction)", ""])
    for i, r in enumerate(intel.forward_playbook, 1):
        lines.append(f"{i}. {r}")

    lines.append("")
    return "\n".join(lines)


def save_overall_outputs(intel: OverallIntel, out_dir: Path) -> Tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{intel.team.lower()}_{intel.from_year}_{intel.to_year}_overall"
    json_path = out_dir / f"{tag}_intelligence.json"
    md_path = out_dir / f"{tag}_intelligence.md"

    payload = {
        "team": intel.team,
        "from_year": intel.from_year,
        "to_year": intel.to_year,
        "summary": intel.summary,
        "strategy_profile": intel.strategy_profile,
        "era_playbooks": intel.era_playbooks,
        "price_band_analysis": intel.price_band_analysis,
        "role_win_rates": intel.role_win_rates,
        "recurring_misses": intel.recurring_misses,
        "career_rivals": intel.career_rivals,
        "what_works": intel.what_works,
        "what_fails": intel.what_fails,
        "strategy_narrative": intel.strategy_narrative,
        "forward_playbook": intel.forward_playbook,
        "yearly_trends": intel.yearly_trends,
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_overall_markdown(intel), encoding="utf-8")
    pd.DataFrame(intel.recurring_misses).to_csv(out_dir / f"{tag}_premium_misses.csv", index=False)
    pd.DataFrame(intel.price_band_analysis).to_csv(out_dir / f"{tag}_price_bands.csv", index=False)
    return json_path, md_path


def print_overall_summary(intel: OverallIntel) -> None:
    s = intel.summary
    sp = intel.strategy_profile
    print("\n" + "=" * 72)
    print(f"  {intel.team} OVERALL AUCTION INTELLIGENCE — {intel.from_year}–{intel.to_year}")
    print("=" * 72)
    print(
        f"  Career: {s.get('targets')} targets | {s.get('won')} won | "
        f"{s.get('win_rate_pct')}% win rate | ₹{s.get('spend_won_cr', 0):.2f} Cr spent"
    )
    print(f"  Archetype: {sp.get('archetype')}")

    print("\n  STRATEGY (inferred)")
    for line in intel.strategy_narrative[:4]:
        print(f"    • {line.replace('**', '')}")

    print("\n  WHAT WORKS")
    for w in intel.what_works[:4]:
        print(f"    ✓ {w}")

    print("\n  WHAT FAILS")
    for f in intel.what_fails[:4]:
        print(f"    ✗ {f}")

    print("\n  FORWARD PLAYBOOK")
    for i, r in enumerate(intel.forward_playbook[:5], 1):
        print(f"    {i}. {r}")
    print("=" * 72 + "\n")


def analyze_team_year(df: pd.DataFrame, team: str, year: int) -> AuctionIntel:
    team = team.upper()
    all_yr = df[df["year"] == year]
    csk_yr = all_yr[all_yr["viewing_team_code"] == team].copy()
    league = league_player_demand(df, year)

    intel = AuctionIntel(team=team, year=year)
    if csk_yr.empty:
        intel.recommendations = [f"No bid data for {team} in {year}."]
        return intel

    won = csk_yr[csk_yr["won"]]
    lost = csk_yr[~csk_yr["won"]]

    intel.summary = {
        "targets": len(csk_yr),
        "won": len(won),
        "lost": len(lost),
        "win_rate_pct": round(100 * len(won) / len(csk_yr), 1),
        "spend_won_cr": round(won["last_bid_cr"].sum(), 2),
        "avg_won_price_cr": round(won["last_bid_cr"].mean(), 2) if len(won) else 0,
        "avg_lost_exit_cr": round(lost["last_bid_cr"].mean(), 2) if len(lost) else 0,
        "overseas_chased": int(csk_yr["overseas"].sum()),
        "overseas_won": int(won["overseas"].sum()),
        "indian_chased": int((~csk_yr["overseas"]).sum()),
        "indian_won": int((~won["overseas"]).sum()),
        "highest_win_cr": round(won["last_bid_cr"].max(), 2) if len(won) else 0,
        "highest_loss_exit_cr": round(lost["last_bid_cr"].max(), 2) if len(lost) else 0,
        "roles_chased": csk_yr["role_bucket"].value_counts().to_dict(),
        "roles_won": won["role_bucket"].value_counts().to_dict(),
    }

    intel.squad_gap_signals = infer_squad_gaps(csk_yr, won)
    intel.won_targets = won.sort_values("last_bid_cr", ascending=False)[
        ["player_name", "role_bucket", "country", "num_bids", "last_bid_cr"]
    ].rename(columns={"role_bucket": "role", "last_bid_cr": "price_cr"}).to_dict(orient="records")
    intel.lost_targets = lost_battle_details(lost, league, all_yr)
    intel.high_demand_misses = high_demand_misses(lost, league)
    intel.rival_head_to_head = rival_head_to_head(csk_yr, all_yr)
    intel.role_win_rates = role_win_rates(csk_yr)

    csk_hist = df[df["viewing_team_code"] == team]
    intel.historical_trends = historical_trends(csk_hist)
    intel.recommendations = build_recommendations(intel)
    return intel


def render_markdown(intel: AuctionIntel) -> str:
    s = intel.summary
    lines = [
        f"# {intel.team} Auction Intelligence — IPL {intel.year}",
        "",
        "## Executive snapshot",
        "",
        f"- **Bid targets:** {s.get('targets', 0)} | **Won:** {s.get('won', 0)} | **Lost:** {s.get('lost', 0)} | **Win rate:** {s.get('win_rate_pct', 0)}%",
        f"- **Spend on wins:** ₹{s.get('spend_won_cr', 0):.2f} Cr | **Avg won price:** ₹{s.get('avg_won_price_cr', 0):.2f} Cr",
        f"- **Overseas chased/won:** {s.get('overseas_chased', 0)}/{s.get('overseas_won', 0)} | **Indian chased/won:** {s.get('indian_chased', 0)}/{s.get('indian_won', 0)}",
        "",
        "## Squad gap signals (from bid behavior)",
        "",
    ]
    if intel.squad_gap_signals:
        for g in intel.squad_gap_signals:
            lines.append(f"- **[{g.get('priority', '?')}]** {g['insight']}")
    else:
        lines.append("- No strong gap signals from bid patterns.")

    lines.extend(["", "## Top recommendations", ""])
    for i, r in enumerate(intel.recommendations, 1):
        lines.append(f"{i}. {r}")

    lines.extend(["", "## Lost battles — who beat CSK & why it mattered", ""])
    lines.append("| Player | Role | CSK exit | Winner | Win price | Demand | Why CSK chased |")
    lines.append("|--------|------|----------|--------|-----------|--------|----------------|")
    for row in intel.lost_targets[:10]:
        lines.append(
            f"| {row['player']} | {row['role']} | {row['csk_last_bid_cr']} Cr | {row.get('winner') or '—'} | "
            f"{row.get('winner_price_cr') or '—'} | {row.get('league_total_bids') or '—'} bids | {row['why_csk_wanted']} |"
        )

    lines.extend(["", "## Role win rates", ""])
    lines.append("| Role | Targets | Won | Win % | Avg won | Avg lost exit |")
    lines.append("|------|---------|-----|-------|---------|---------------|")
    for r in intel.role_win_rates:
        lines.append(
            f"| {r['role']} | {r['targets']} | {r['won']} | {r['win_rate_pct']}% | "
            f"{r['avg_price_won_cr']} Cr | {r['avg_price_lost_cr']} Cr |"
        )

    lines.extend(["", "## Franchise rivals (won players CSK lost)", ""])
    for rival in intel.rival_head_to_head[:6]:
        lines.append(f"- **{rival['rival']}** — {rival['players_won_vs_csk']} wins vs CSK: {', '.join(rival['examples'])}")

    lines.extend(["", "## Key acquisitions (won)", ""])
    for w in intel.won_targets[:8]:
        lines.append(
            f"- **{w['player_name']}** ({w['role']}, {w['country']}) — ₹{w['price_cr']:.2f} Cr after {w['num_bids']} bids"
        )

    if intel.historical_trends.get("by_year"):
        lines.extend(["", "## CSK historical bid trends", ""])
        lines.append("| Year | Targets | Won | Win % | Spend (won) | Top role chased |")
        lines.append("|------|---------|-----|-------|-------------|-----------------|")
        for y in intel.historical_trends["by_year"]:
            lines.append(
                f"| {y['year']} | {y['targets']} | {y['won']} | {y['win_rate_pct']}% | "
                f"₹{y['spend_won_cr']:.2f} Cr | {y['top_role_chased']} |"
            )

    lines.append("")
    return "\n".join(lines)


def save_outputs(intel: AuctionIntel, out_dir: Path) -> Tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{intel.team.lower()}_{intel.year}_intelligence.json"
    md_path = out_dir / f"{intel.team.lower()}_{intel.year}_intelligence.md"

    payload = {
        "team": intel.team,
        "year": intel.year,
        "summary": intel.summary,
        "squad_gap_signals": intel.squad_gap_signals,
        "won_targets": intel.won_targets,
        "lost_targets": intel.lost_targets,
        "high_demand_misses": intel.high_demand_misses,
        "rival_head_to_head": intel.rival_head_to_head,
        "role_win_rates": intel.role_win_rates,
        "recommendations": intel.recommendations,
        "historical_trends": intel.historical_trends,
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_markdown(intel), encoding="utf-8")

    # CSV slices for dashboard import
    pd.DataFrame(intel.lost_targets).to_csv(out_dir / f"{intel.team.lower()}_{intel.year}_lost_battles.csv", index=False)
    pd.DataFrame(intel.role_win_rates).to_csv(out_dir / f"{intel.team.lower()}_{intel.year}_role_win_rates.csv", index=False)
    return json_path, md_path


def print_console_summary(intel: AuctionIntel) -> None:
    s = intel.summary
    print("\n" + "=" * 72)
    print(f"  {intel.team} AUCTION INTELLIGENCE — IPL {intel.year}")
    print("=" * 72)
    print(f"  Targets: {s.get('targets')} | Won: {s.get('won')} | Lost: {s.get('lost')} | Win rate: {s.get('win_rate_pct')}%")
    print(f"  Spend (won): ₹{s.get('spend_won_cr', 0):.2f} Cr")

    print("\n  SQUAD GAP SIGNALS")
    for g in intel.squad_gap_signals[:5]:
        print(f"    [{g.get('priority')}] {g['insight']}")

    print("\n  TOP LOST BATTLES")
    for row in intel.lost_targets[:5]:
        print(
            f"    • {row['player']} ({row['role']}) — CSK exit ₹{row['csk_last_bid_cr']} Cr → "
            f"{row.get('winner', '?')} ₹{row.get('winner_price_cr', '?')} Cr | {row['why_csk_wanted']}"
        )

    print("\n  ACTION ITEMS")
    for i, r in enumerate(intel.recommendations[:5], 1):
        print(f"    {i}. {r}")
    print("=" * 72 + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="CSK auction bid-war intelligence")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--team", default="CSK", help="Franchise code (default CSK)")
    parser.add_argument("--year", type=int, default=2026, help="Focus auction year (single-year mode)")
    parser.add_argument("--overall", action="store_true", help="Career analysis across from-year..to-year")
    parser.add_argument("--from-year", type=int, default=2018)
    parser.add_argument("--to-year", type=int, default=2026)
    parser.add_argument("--out-dir", type=Path, default=REPORT_DIR)
    args = parser.parse_args()

    df = load_bids(args.csv, args.db, args.from_year, args.to_year)

    if args.overall:
        overall = analyze_team_overall(df, args.team, args.from_year, args.to_year)
        json_path, md_path = save_overall_outputs(overall, args.out_dir)
        print_overall_summary(overall)
        print(f"Overall reports saved:\n  {md_path}\n  {json_path}")
        return 0

    intel = analyze_team_year(df, args.team, args.year)
    json_path, md_path = save_outputs(intel, args.out_dir)
    print_console_summary(intel)
    print(f"Reports saved:\n  {md_path}\n  {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
