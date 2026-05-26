"""
War Room decision engine — blends valuation, squad gaps, and bid-history intelligence.
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
BIDS_CSV = ROOT / "data" / "cricbuzz_all_bids" / "all_bids_master.csv"

from auction_constants import IPL_PURSE_CR

IDEAL_ROLE = {"Batter": 6, "Bowler": 6, "All Rounder": 5, "Wicket Keeper": 2}
MAX_OVERSEAS = 8


def normalize_role(role: str) -> str:
    r = (role or "").strip()
    if "WK" in r.upper() or r in ("Wicket Keeper", "Wicketkeeper", "WK-Batter", "WK Batter", "WK-Batsman"):
        return "Wicket Keeper"
    if r in ("Allrounder", "All-Rounder", "All Rounder"):
        return "All Rounder"
    if r in ("Batter", "Batsman"):
        return "Batter"
    if r in ("Bowler", "Bowling"):
        return "Bowler"
    return r or "Unknown"


def is_overseas(country: str) -> bool:
    c = (country or "").strip().lower()
    return c not in ("", "india", "indian")


@lru_cache(maxsize=1)
def load_bids_df() -> pd.DataFrame:
    if not BIDS_CSV.exists():
        return pd.DataFrame()
    df = pd.read_csv(BIDS_CSV)
    df["viewing_team_code"] = df["viewing_team_code"].fillna("").astype(str).str.strip().str.upper()
    df["player_name"] = df["player_name"].astype(str).str.strip()
    df["role_bucket"] = df["role"].map(normalize_role)
    df["overseas"] = df["country"].map(is_overseas)
    df["num_bids"] = pd.to_numeric(df.get("num_bids", 0), errors="coerce").fillna(0).astype(int)
    df["last_bid_cr"] = pd.to_numeric(df.get("last_bid_cr", 0), errors="coerce").fillna(0.0)
    df["won"] = df["bid_war"].fillna("").astype(str).str.lower() == "won"
    return df


def csk_strategy_summary(team: str = "CSK", from_year: int = 2018, to_year: int = 2026) -> Dict[str, Any]:
    df = load_bids_df()
    csk = df[(df["viewing_team_code"] == team) & df["year"].between(from_year, to_year)]
    if csk.empty:
        return {"available": False}

    won = csk[csk["won"]]
    value_wins = won[won["last_bid_cr"] <= 1.0]
    premium = csk[csk["last_bid_cr"] >= 5.0]
    premium_won = premium[premium["won"]]

    return {
        "available": True,
        "team": team,
        "from_year": from_year,
        "to_year": to_year,
        "archetype": "Bowling-depth value builder",
        "career_win_rate_pct": round(100 * len(won) / len(csk), 1),
        "value_band_win_rate_pct": round(100 * len(value_wins) / max(1, len(csk[csk["last_bid_cr"] <= 1.0])), 1),
        "premium_win_rate_pct": round(100 * len(premium_won) / max(1, len(premium)), 1),
        "snipe_win_pct": round(100 * (won["num_bids"] <= 1).sum() / max(1, len(won)), 1),
        "top_chased_role": csk["role_bucket"].value_counts().index[0],
        "career_rivals": _career_rivals(csk, df, team)[:5],
        "priority_roles": ["Bowler", "All Rounder", "Wicket Keeper"],
        "playbook": [
            "Tier A: max 2 marquee slots with hard walk-away ceilings.",
            "Tier B: value snipes at ≤₹1 Cr (84% historical win rate).",
            "Avoid premium bid wars unless role is a declared critical gap.",
            "Indian core converts better than overseas marquees — lead or don't enter.",
        ],
    }


def _career_rivals(csk: pd.DataFrame, df: pd.DataFrame, team: str) -> List[Dict[str, Any]]:
    from collections import Counter

    counter: Counter = Counter()
    for _, row in csk[~csk["won"]].iterrows():
        winners = df[
            (df["year"] == row["year"])
            & (df["player_name"] == row["player_name"])
            & (df["won"])
            & (df["viewing_team_code"] != team)
        ]
        for _, w in winners.iterrows():
            counter[w["viewing_team_code"]] += 1
    return [{"rival": r, "wins_vs_csk": c} for r, c in counter.most_common(8)]


def similar_bid_comps(role: str, fmv: float, year: int = 2026, limit: int = 5) -> List[Dict[str, Any]]:
    """Historical bid wars for same role near FMV band."""
    df = load_bids_df()
    yr = df[df["year"] == year]
    role = normalize_role(role)
    band_lo, band_hi = max(0.2, fmv * 0.5), fmv * 1.8
    comps = yr[(yr["role_bucket"] == role) & (yr["last_bid_cr"].between(band_lo, band_hi))]
    if comps.empty:
        comps = yr[yr["role_bucket"] == role]
    comps = comps.sort_values("num_bids", ascending=False).drop_duplicates("player_name").head(limit)
    return [
        {
            "player": r["player_name"],
            "role": r["role_bucket"],
            "num_bids": int(r["num_bids"]),
            "final_price_cr": round(float(r["last_bid_cr"]), 2),
            "winner_team": r["viewing_team_code"],
            "csk_bid": r["viewing_team_code"] == "CSK",
            "csk_result": "won" if r["viewing_team_code"] == "CSK" and r["won"] else (
                "lost" if r["viewing_team_code"] == "CSK" else "—"
            ),
        }
        for _, r in comps.iterrows()
    ]


def likely_competitors(role: str, overseas: bool, year: int = 2026) -> List[Dict[str, Any]]:
    """Teams active on same role profile in recent auction."""
    df = load_bids_df()
    yr = df[(df["year"] == year) & (df["role_bucket"] == normalize_role(role))]
    if yr.empty:
        yr = df[df["role_bucket"] == normalize_role(role)].sort_values("year", ascending=False).head(200)

    if overseas:
        yr = yr[yr["overseas"]]
    team_stats = (
        yr.groupby("viewing_team_code")
        .agg(
            targets=("player_name", "count"),
            wins=("won", "sum"),
            avg_bids=("num_bids", "mean"),
            avg_price=("last_bid_cr", "mean"),
        )
        .reset_index()
    )
    team_stats = team_stats[team_stats["viewing_team_code"] != "CSK"].sort_values(
        ["targets", "avg_bids"], ascending=False
    )
    out = []
    for _, r in team_stats.head(6).iterrows():
        intensity = "VERY HIGH" if r["avg_bids"] >= 8 else "HIGH" if r["avg_bids"] >= 4 else "MEDIUM"
        out.append(
            {
                "team": r["viewing_team_code"],
                "targets_in_role": int(r["targets"]),
                "avg_bids": round(float(r["avg_bids"]), 1),
                "avg_price_cr": round(float(r["avg_price"]), 2),
                "threat_level": intensity,
            }
        )
    return out


def player_bid_history(player_name: str) -> Optional[Dict[str, Any]]:
    df = load_bids_df()
    rows = df[df["player_name"].str.lower() == player_name.strip().lower()]
    if rows.empty:
        # fuzzy: contains
        rows = df[df["player_name"].str.contains(player_name.strip(), case=False, na=False)]
    if rows.empty:
        return None
    csk_rows = rows[rows["viewing_team_code"] == "CSK"]
    latest = rows.sort_values("year", ascending=False).iloc[0]
    return {
        "player": latest["player_name"],
        "ever_targeted": len(rows),
        "csk_targeted": len(csk_rows),
        "latest_year": int(latest["year"]),
        "latest_result": latest["bid_war"],
        "latest_price_cr": round(float(latest["last_bid_cr"]), 2),
        "latest_bids": int(latest["num_bids"]),
        "history": rows.sort_values("year", ascending=False)[
            ["year", "viewing_team_code", "num_bids", "last_bid_cr", "bid_war"]
        ].head(5).to_dict(orient="records"),
    }


def squad_gap_from_list(
    squad: List[Dict[str, Any]], budget: float = IPL_PURSE_CR
) -> Dict[str, Any]:
    role_counts = {"Batter": 0, "Bowler": 0, "All Rounder": 0, "Wicket Keeper": 0}
    overseas = 0
    spent = 0.0
    for p in squad:
        role = normalize_role(p.get("role") or "Unknown")
        if role in role_counts:
            role_counts[role] += 1
        country = (p.get("country") or "").lower()
        if country and country not in ("india", "indian"):
            overseas += 1
        spent += float(p.get("price") or 0)

    gaps = []
    for role, ideal in IDEAL_ROLE.items():
        need = ideal - role_counts.get(role, 0)
        if need > 0:
            gaps.append(
                {
                    "role": role,
                    "have": role_counts.get(role, 0),
                    "ideal": ideal,
                    "need": need,
                    "priority": "Critical" if need >= 2 else "High",
                }
            )
    gaps.sort(key=lambda g: -g["need"])
    return {
        "squad_size": len(squad),
        "role_counts": role_counts,
        "overseas": overseas,
        "overseas_slots_left": MAX_OVERSEAS - overseas,
        "spent_cr": round(spent, 2),
        "remaining_budget_cr": round(budget - spent, 2),
        "gaps": gaps,
        "critical_gaps": [g for g in gaps if g["priority"] == "Critical"],
    }


def _price_band_win_rate(team: str, price: float) -> float:
    df = load_bids_df()
    csk = df[df["viewing_team_code"] == team]
    if price <= 1.0:
        band = csk[csk["last_bid_cr"] <= 1.0]
    elif price <= 5.0:
        band = csk[(csk["last_bid_cr"] > 1.0) & (csk["last_bid_cr"] <= 5.0)]
    elif price <= 10.0:
        band = csk[(csk["last_bid_cr"] > 5.0) & (csk["last_bid_cr"] <= 10.0)]
    else:
        band = csk[csk["last_bid_cr"] > 10.0]
    if band.empty:
        return 50.0
    return round(100 * band["won"].sum() / len(band), 1)


def build_war_room_decision(
    valuation: Dict[str, Any],
    squad_state: Dict[str, Any],
    *,
    current_bid: float = 0.0,
    base_price: float = 2.0,
    auction_year: int = 2026,
    team: str = "CSK",
) -> Dict[str, Any]:
    """Merge valuation + squad gaps + bid intelligence into war-room card."""
    role = normalize_role(valuation.get("role") or valuation.get("role_detail") or "Unknown")
    country = valuation.get("country") or ""
    overseas = is_overseas(country)
    fmv = float(valuation.get("estimated_value") or 2.5)
    floor = float(valuation.get("floor_price") or max(0.2, fmv * 0.6))
    ceiling = float(valuation.get("ceiling_price") or fmv * 1.4)
    fit = float(valuation.get("csk_fit_score") or 50)
    form = float(valuation.get("form_score") or 50)
    remaining = float(squad_state.get("remaining_budget_cr") or IPL_PURSE_CR)

    # Role gap boost
    role_gap = next((g for g in squad_state.get("gaps", []) if g["role"] == role), None)
    gap_need = role_gap["need"] if role_gap else 0
    gap_priority = role_gap["priority"] if role_gap else "Low"

    comps = similar_bid_comps(role, fmv, year=auction_year)
    avg_bids = sum(c["num_bids"] for c in comps) / max(len(comps), 1)
    bid_war_prob = min(95, int(40 + avg_bids * 4))
    competitors = likely_competitors(role, overseas, year=auction_year)
    band_wr = _price_band_win_rate(team, fmv)
    hist = player_bid_history(valuation.get("player_name", ""))

    # Walk-away: ceiling adjusted by CSK historical success in band
    if band_wr < 35:
        walk_away = round(min(ceiling * 0.95, fmv * 1.25), 2)
    elif gap_need >= 2:
        walk_away = round(min(ceiling * 1.05, remaining * 0.35), 2)
    else:
        walk_away = round(min(ceiling, fmv * 1.35), 2)
    walk_away = min(walk_away, remaining)

    entry = round(max(base_price, floor, fmv * 0.55), 2)
    expected_lo = round(fmv * 0.85, 2)
    expected_hi = round(fmv * 1.25, 2)

    # Should bid logic
    if fit < 45 or walk_away < base_price:
        should_bid = "NO"
        strategy = "AVOID"
        confidence = 75
    elif gap_need >= 2 and fit >= 60:
        should_bid = "YES"
        strategy = "AGGRESSIVE" if fit >= 72 else "MEASURED"
        confidence = min(92, int(60 + fit * 0.3 + gap_need * 5))
    elif fit >= 55 and walk_away <= remaining:
        should_bid = "YES" if fit >= 65 else "MAYBE"
        strategy = "MEASURED" if band_wr >= 40 else "CAUTIOUS"
        confidence = min(88, int(50 + fit * 0.35))
    else:
        should_bid = "MAYBE" if fit >= 50 else "NO"
        strategy = "CAUTIOUS"
        confidence = int(45 + fit * 0.25)

    if overseas and squad_state.get("overseas_slots_left", 8) <= 0:
        should_bid = "NO"
        strategy = "AVOID"
        confidence = 90

    if band_wr < 32 and fmv >= 5:
        strategy = "CAUTIOUS"
        if should_bid == "YES":
            should_bid = "MAYBE"

    # Live bid recommendation
    live = _live_bid_advice(current_bid, entry, fmv, walk_away, should_bid)

    # Budget impact
    win_price = current_bid if current_bid > 0 else fmv
    role_counts = dict(squad_state.get("role_counts") or {})
    win_impact = {
        "if_win_at_cr": round(win_price, 2),
        "squad_size_after": squad_state.get("squad_size", 0) + 1,
        "role_after": f"{role_counts.get(role, 0) + 1}/{IDEAL_ROLE.get(role, '?')}",
        "budget_after_cr": round(remaining - win_price, 2),
        "on_track": remaining - win_price >= remaining * 0.4 or gap_need >= 2,
    }
    lose_impact = {
        "squad_size": squad_state.get("squad_size", 0),
        "role_still_need": gap_need,
        "budget_unchanged_cr": round(remaining, 2),
        "next_action": f"Find alternate {role}" if gap_need > 0 else "Monitor next lot",
    }

    reasons = []
    if gap_need >= 2:
        reasons.append(f"Critical squad gap: need {gap_need} more {role}(s)")
    if fit >= 70:
        reasons.append(f"Strong CSK fit ({fit}%)")
    if form >= 70:
        reasons.append(f"Hot form ({form}%)")
    if band_wr >= 50:
        reasons.append(f"CSK wins {band_wr}% of bids in this price band")
    if avg_bids >= 8:
        reasons.append(f"Expect bid war (~{int(avg_bids)} bids based on comps)")
    if band_wr < 35:
        reasons.append(f"CSK weak in premium band ({band_wr}% win rate) — strict cap")
    if hist and hist.get("csk_targeted"):
        reasons.append(f"CSK chased before ({hist['latest_result']} @ ₹{hist['latest_price_cr']} Cr)")

    return {
        "player_name": valuation.get("player_name"),
        "role": role,
        "country": country,
        "overseas": overseas,
        "quick_decision": {
            "should_bid": should_bid,
            "entry_bid_cr": entry,
            "fair_market_value_cr": round(fmv, 2),
            "walk_away_cr": walk_away,
            "strategy": strategy,
            "confidence_pct": confidence,
            "one_liner": _one_liner(should_bid, strategy, walk_away, gap_need),
        },
        "player_analysis": {
            "form_score": form,
            "csk_fit_score": fit,
            "auction_verdict": valuation.get("auction_verdict"),
            "base_price_cr": base_price,
            "market_band": valuation.get("market_value"),
            "historical_auction_price": valuation.get("historical_auction_price"),
        },
        "bidding_intelligence": {
            "expected_bids_range": f"{max(1, int(avg_bids - 2))}–{int(avg_bids + 4)}",
            "bid_war_probability_pct": bid_war_prob,
            "expected_final_price_cr": f"{expected_lo}–{expected_hi}",
            "csk_band_win_rate_pct": band_wr,
            "similar_players": comps,
            "likely_competitors": competitors,
            "player_bid_history": hist,
        },
        "squad_context": squad_state,
        "budget_impact": {"if_win": win_impact, "if_lose": lose_impact},
        "live_bid": live,
        "reasons": reasons,
        "data_sources": ["valuation_engine", "bid_history_2018_2026", "squad_state"],
    }


def _one_liner(should_bid: str, strategy: str, walk_away: float, gap_need: int) -> str:
    if should_bid == "NO":
        return "Skip — does not clear fit/gap/budget thresholds."
    if should_bid == "MAYBE":
        return f"Conditional — only if price stays below ₹{walk_away} Cr."
    if strategy == "AGGRESSIVE":
        return f"Bid to win — fills critical gap ({gap_need} needed); cap ₹{walk_away} Cr."
    return f"Push to FMV, hard stop at ₹{walk_away} Cr."


def _live_bid_advice(
    current_bid: float, entry: float, fmv: float, walk_away: float, should_bid: str
) -> Dict[str, Any]:
    if current_bid <= 0:
        return {
            "status": "WAIT",
            "message": f"Enter at ≥₹{entry} Cr when lot opens.",
            "recommended_bid_cr": entry,
            "should_bid_now": should_bid in ("YES", "MAYBE"),
        }
    next_bid = round(current_bid + 0.25, 2)
    if current_bid > walk_away:
        return {
            "status": "DROP OUT",
            "message": f"₹{current_bid} Cr exceeds walk-away ₹{walk_away} Cr — exit.",
            "recommended_bid_cr": None,
            "should_bid_now": False,
        }
    if current_bid <= fmv:
        return {
            "status": "ACTIVE",
            "message": f"₹{current_bid} Cr is at/below FMV — bid ₹{next_bid} Cr.",
            "recommended_bid_cr": next_bid,
            "should_bid_now": True,
        }
    if current_bid <= walk_away:
        return {
            "status": "CAUTION",
            "message": f"Above FMV — bid ₹{next_bid} Cr only if gap is critical; cap ₹{walk_away} Cr.",
            "recommended_bid_cr": next_bid,
            "should_bid_now": current_bid < walk_away * 0.95,
        }
    return {"status": "DROP OUT", "message": "Walk-away reached.", "should_bid_now": False}
