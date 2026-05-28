"""
Franchise-grade IPL auction valuation pipeline.

stats → role → market normalization → Bayesian shrinkage → uncertainty → price band
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ── Market & role constants ────────────────────────────────────────────────────

ROLE_SCARCITY = {
    "Death Bowler": 1.35,
    "Finisher": 1.28,
    "Left Arm Pacer": 1.25,
    "Wrist Spinner": 1.18,
    "Powerplay Bowler": 1.12,
    "Anchor": 1.05,
    "Fast Bowling AR": 1.22,
    "WK Finisher": 1.20,
    "All Rounder": 1.15,
    "Batter": 1.00,
    "Bowler": 1.00,
    "Player": 1.00,
}

PRIOR_WEIGHT = 25  # Bayesian prior strength (matches-equivalent)
EXPERIENCE_MATCHES_CAP = 50

PRICE_MIN = 0.20
PRICE_MAX = 20.0


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _percentile(sorted_vals: List[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


# ── Auction market model ───────────────────────────────────────────────────────

class AuctionMarketModel:
    """Derive IPL auction distributions and role premiums from historical prices."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._global_dist: Optional[Dict] = None
        self._role_dist: Dict[str, Dict] = {}
        self._role_premium: Dict[str, float] = {}

    def _load_prices(self, role_filter: Optional[str] = None) -> List[float]:
        if role_filter:
            rows = self.conn.execute(
                "SELECT price FROM auction_prices WHERE price > 0 AND role = ?",
                (role_filter,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT price FROM auction_prices WHERE price > 0"
            ).fetchall()
        return [float(r[0]) for r in rows if r[0]]

    def get_market_distribution(self, role: Optional[str] = None) -> Dict:
        cache_key = role or "__global__"
        if role and cache_key in self._role_dist:
            return self._role_dist[cache_key]
        if not role and self._global_dist is not None:
            return self._global_dist

        prices = sorted(self._load_prices(role))
        if not prices:
            fallback = {
                "mean": 2.5,
                "std": 2.0,
                "p25": 1.0,
                "p50": 2.0,
                "p75": 4.0,
                "p90": 8.0,
                "count": 0,
            }
            if role:
                self._role_dist[cache_key] = fallback
            else:
                self._global_dist = fallback
            return fallback

        n = len(prices)
        mean = sum(prices) / n
        variance = sum((p - mean) ** 2 for p in prices) / n
        std = math.sqrt(variance) if variance > 0 else 0.5

        dist = {
            "mean": round(mean, 3),
            "std": round(max(std, 0.3), 3),
            "p25": round(_percentile(prices, 0.25), 3),
            "p50": round(_percentile(prices, 0.50), 3),
            "p75": round(_percentile(prices, 0.75), 3),
            "p90": round(_percentile(prices, 0.90), 3),
            "count": n,
        }
        if role:
            self._role_dist[cache_key] = dist
        else:
            self._global_dist = dist
        return dist

    def get_role_premium(self, role: str) -> float:
        if role in self._role_premium:
            return self._role_premium[role]

        global_dist = self.get_market_distribution()
        role_map = {
            "Finisher": "Batter",
            "Anchor": "Batter",
            "Death Bowler": "Bowler",
            "Powerplay Bowler": "Bowler",
            "Wrist Spinner": "Bowler",
            "Left Arm Pacer": "Bowler",
            "Fast Bowling AR": "All Rounder",
            "WK Finisher": "Wicket Keeper",
            "All Rounder": "All Rounder",
            "Batter": "Batter",
            "Bowler": "Bowler",
            "Player": None,
        }
        auction_role = role_map.get(role)
        if not auction_role:
            premium = 1.0
        else:
            role_dist = self.get_market_distribution(auction_role)
            if role_dist["count"] > 5 and global_dist["mean"] > 0:
                premium = role_dist["mean"] / global_dist["mean"]
            else:
                premium = 1.0

        scarcity = ROLE_SCARCITY.get(role, 1.0)
        combined = _clamp((premium + scarcity) / 2, 0.85, 1.45)
        self._role_premium[role] = round(combined, 3)
        return self._role_premium[role]

    def get_historical_price(self, player_name: str) -> Optional[float]:
        """Match auction history by surname token (avoids Surya/Suryavanshi false positives)."""
        parts = player_name.strip().split()
        if not parts:
            return None
        surname = parts[-1].lower()
        if len(surname) < 3:
            return None

        rows = self.conn.execute(
            """
            SELECT price, player_name, year FROM auction_prices
            WHERE LOWER(player_name) LIKE ? AND price > 0
            ORDER BY year DESC
            LIMIT 12
            """,
            (f"%{surname}%",),
        ).fetchall()

        prices = []
        for price, aname, _year in rows:
            tokens = aname.lower().split()
            if not tokens:
                continue
            if tokens[-1] == surname or (len(tokens) >= 2 and tokens[-1].startswith(surname[:4])):
                prices.append(float(price))

        if not prices:
            return None
        # Ignore base retention slots (₹0.2 Cr); use real auction clears
        meaningful = sorted([p for p in prices if p >= 1.0], reverse=True)[:5]
        if not meaningful:
            meaningful = sorted(prices, reverse=True)[:3]
        mid = len(meaningful) // 2
        return (
            meaningful[mid]
            if len(meaningful) % 2
            else (meaningful[mid - 1] + meaningful[mid]) / 2
        )


# ── Role inference ─────────────────────────────────────────────────────────────

class RoleInferenceEngine:
    """Infer specialist IPL roles from performance metrics (not runs/wickets buckets)."""

    def infer(self, player: Dict) -> Tuple[str, str]:
        runs = player.get("total_runs", 0) or 0
        wickets = player.get("total_wickets", 0) or 0
        sr = float(player.get("strike_rate", 0) or 0)
        last_sr = float(player.get("last_10_matches_sr", 0) or 0)
        econ = float(player.get("economy_rate", 10) or 10)
        last_econ = float(player.get("last_10_matches_economy", 10) or 10)
        avg = float(player.get("average", 0) or 0)
        matches = player.get("matches_played", 0) or 0

        effective_sr = last_sr if last_sr > 0 else sr
        effective_econ = last_econ if 0 < last_econ < 20 else econ

        # Bowling specialists first
        if wickets >= 15:
            if effective_econ <= 8.0 and wickets >= 20:
                return "Death Bowler", "Elite death-phase economy with wicket threat"
            if effective_econ <= 9.0:
                return "Powerplay Bowler", "Economical new-ball / early overs profile"
            if wickets >= 5 and runs < 200:
                return "Bowler", "Specialist bowling workload"
            return "Bowler", "Primary bowler"

        # Batting specialists
        if runs >= 150 or effective_sr >= 130:
            if effective_sr >= 170 and (matches <= 30 or runs / max(matches, 1) >= 25):
                return "Finisher", f"High-impact SR ({effective_sr:.0f}) — death/finisher profile"
            if avg >= 35 and effective_sr < 140:
                return "Anchor", f"Stable average ({avg:.1f}) with controlled SR"
            if runs >= 400:
                return "Batter", "Established top-order run scorer"
            if effective_sr >= 150:
                return "Finisher", f"Aggressive striker (SR {effective_sr:.0f})"
            return "Batter", "Batting-first profile"

        # All-rounders
        if runs >= 200 and wickets >= 5:
            if wickets >= 10:
                return "Fast Bowling AR", "Dual skill — runs and bowling wickets"
            return "All Rounder", "Contributions with bat and ball"

        if runs > wickets * 30:
            return "Batter", "Limited bowling — batting utility"
        if wickets > 0:
            return "Bowler", "Limited batting — bowling utility"

        return "Player", "Insufficient data for specialist role"


# ── Risk & uncertainty ─────────────────────────────────────────────────────────

class RiskEngine:
    """Sample-size adjustment, confidence, and volatility for price bands."""

    def analyze(self, player: Dict) -> Dict:
        matches = player.get("matches_played", 0) or 0
        form = float(player.get("form_rating", 50) or 50)

        experience_factor = min(1.0, matches / EXPERIENCE_MATCHES_CAP)
        confidence = min(95, int(matches * 2 + form * 0.15))

        if matches < 15:
            volatility = "High"
            vol_mult = 1.45
        elif matches < 35:
            volatility = "Medium"
            vol_mult = 1.20
        else:
            volatility = "Low"
            vol_mult = 1.05

        return {
            "experience_factor": round(experience_factor, 3),
            "confidence": confidence,
            "volatility": volatility,
            "volatility_multiplier": vol_mult,
            "matches_played": matches,
        }

    @staticmethod
    def bayesian_shrink(value: float, sample: float, prior: float, prior_weight: float = PRIOR_WEIGHT) -> float:
        """Shrink player metric toward league prior based on sample size."""
        if sample <= 0:
            return prior
        return (value * sample + prior * prior_weight) / (sample + prior_weight)


# ── CSK fit engine ─────────────────────────────────────────────────────────────

class CSKFitEngine:
    """Structured CSK squad fit: Indian core, death skills, spin, anchor, Chepauk profile."""

    def score(self, player: Dict, role: str, metadata: Dict) -> Tuple[float, List[str]]:
        result = self.score_with_breakdown(player, role, metadata)
        return result["final_score"], result["reasons"]

    def score_with_breakdown(
        self, player: Dict, role: str, metadata: Dict
    ) -> Dict:
        score = 50.0
        reasons: List[str] = []
        breakdown: List[Dict] = [
            {"step": "base", "label": "Base", "delta": 0.0, "running_total": 50.0}
        ]

        def add(step: str, label: str, delta: float, reason: str | None = None) -> None:
            nonlocal score
            score += delta
            if reason:
                reasons.append(reason)
            breakdown.append(
                {
                    "step": step,
                    "label": label,
                    "delta": round(delta, 1),
                    "running_total": round(score, 1),
                    "reason": reason,
                }
            )

        form = float(player.get("form_rating", 50) or 50)
        last_sr = float(player.get("last_10_matches_sr", 0) or 0)
        last_econ = float(player.get("last_10_matches_economy", 10) or 10)
        matches = player.get("matches_played", 0) or 0
        country = (player.get("country") or "").lower()
        ipl_exp = metadata.get("ipl_experience", "Moderate")

        exp = min(1.0, matches / 40)
        adj_form = form * (0.6 + 0.4 * exp)
        breakdown.append(
            {
                "step": "adj_form",
                "label": "Experience-adjusted form",
                "delta": 0.0,
                "running_total": round(score, 1),
                "detail": f"raw={form:.1f}, adj={adj_form:.1f} (matches={matches})",
            }
        )
        if adj_form >= 75:
            add("form_hot", "Hot form (adj ≥75)", 18, f"Hot form ({form:.0f}/100, sample-adjusted)")
        elif adj_form >= 60:
            add("form_good", "Good form (adj ≥60)", 10, f"Good recent form ({form:.0f}/100)")
        elif adj_form < 40:
            add("form_cold", "Cold form (adj <40)", -8, f"Below-par form ({form:.0f}/100)")

        if role in ("Death Bowler", "Powerplay Bowler", "Bowler") and 0 < last_econ <= 8.5:
            add(
                "death_econ",
                f"Death/economy asset (econ {last_econ:.1f})",
                15,
                f"Death/economy asset (econ {last_econ:.1f})",
            )
        elif role in ("Death Bowler", "Bowler") and last_econ > 10:
            add(
                "death_expensive",
                f"Expensive death phases (econ {last_econ:.1f})",
                -10,
                f"Expensive in death phases (econ {last_econ:.1f})",
            )

        if role in ("Finisher", "WK Finisher") and last_sr >= 155:
            add(
                "finisher_sr",
                f"Finisher SR ({last_sr:.0f})",
                12,
                f"Finisher SR fits CSK death batting ({last_sr:.0f})",
            )
        elif role == "Anchor" and float(player.get("average", 0) or 0) >= 30:
            add("anchor", "Anchor stability", 8, "Anchor stability for middle-order rebuild")

        if role in ("Wrist Spinner",) or (
            role == "Bowler" and float(player.get("economy_rate", 10) or 10) <= 8.5
        ):
            add("spin", "Spin / Chepauk", 6, "Spin-friendly Chepauk profile")

        if country in ("india", "", "indian", "ind"):
            add("indian", "Indian core", 8, "Indian core — fits CSK retention strategy")

        if ipl_exp == "Experienced" and matches >= 50:
            add("ipl_exp", "IPL experience", 6, "IPL experience — plug-and-play")
        elif matches < 12:
            add("low_sample", "Limited IPL sample", -5, "Limited IPL sample — development risk")

        if metadata.get("big_game_performer"):
            add("big_game", "Big-match temperament", 5, "Big-match temperament")

        injury = metadata.get("injury_risk", "Medium")
        if injury == "High":
            add("injury_high", "Injury risk high", -8, "High injury risk")
        elif injury == "Low":
            add("injury_low", "Injury risk low", 3, None)

        final = round(_clamp(score, 0, 100), 1)
        return {
            "final_score": final,
            "reasons": reasons[:6],
            "breakdown": breakdown,
            "inputs": {
                "role": role,
                "form_rating_raw": form,
                "adj_form": round(adj_form, 1),
                "last_10_sr": last_sr,
                "last_10_economy": last_econ,
                "matches_played": matches,
                "country": country or "unknown",
                "ipl_experience": ipl_exp,
                "injury_risk": injury,
            },
        }


# ── Player performance scoring ─────────────────────────────────────────────────

class PerformanceScorer:
    """Build a 0–100 talent score with Bayesian shrinkage toward league priors."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._league_priors: Optional[Dict] = None

    def _league_averages(self) -> Dict:
        if self._league_priors:
            return self._league_priors
        row = self.conn.execute(
            """
            SELECT
                AVG(CASE WHEN matches_played > 0 THEN CAST(total_runs AS REAL) / matches_played ELSE 0 END),
                AVG(strike_rate),
                AVG(form_rating),
                AVG(CASE WHEN matches_played > 0 THEN CAST(total_wickets AS REAL) / matches_played ELSE 0 END),
                AVG(economy_rate)
            FROM player_auction_stats
            WHERE matches_played >= 5
            """
        ).fetchone()
        self._league_priors = {
            "runs_per_match": float(row[0] or 18),
            "strike_rate": float(row[1] or 130),
            "form": float(row[2] or 50),
            "wickets_per_match": float(row[3] or 0.8),
            "economy": float(row[4] or 9.0),
        }
        return self._league_priors

    def raw_score(self, player: Dict, role: str) -> float:
        priors = self._league_averages()
        matches = max(player.get("matches_played", 0) or 0, 1)
        runs = player.get("total_runs", 0) or 0
        wickets = player.get("total_wickets", 0) or 0
        form = float(player.get("form_rating", 50) or 50)
        sr = float(player.get("strike_rate", 0) or player.get("last_10_matches_sr", 0) or 0)
        last_sr = float(player.get("last_10_matches_sr", 0) or sr)
        econ = float(player.get("economy_rate", 10) or 10)
        last_econ = float(player.get("last_10_matches_economy", 10) or econ)

        rpm = runs / matches
        wpm = wickets / matches

        shrunk_rpm = RiskEngine.bayesian_shrink(rpm, matches, priors["runs_per_match"])
        shrunk_sr = RiskEngine.bayesian_shrink(last_sr if last_sr else sr, matches, priors["strike_rate"])
        shrunk_form = RiskEngine.bayesian_shrink(form, matches, priors["form"])
        shrunk_wpm = RiskEngine.bayesian_shrink(wpm, matches, priors["wickets_per_match"])
        shrunk_econ = RiskEngine.bayesian_shrink(
            last_econ if 0 < last_econ < 20 else econ, matches, priors["economy"]
        )

        # Role-weighted components (0–100 scale)
        if role in ("Death Bowler", "Powerplay Bowler", "Bowler", "Wrist Spinner", "Left Arm Pacer"):
            econ_score = _clamp((12 - shrunk_econ) / 4 * 50, 0, 50)
            wkt_score = _clamp(shrunk_wpm / 2 * 50, 0, 50)
            return econ_score + wkt_score

        if role in ("Finisher", "WK Finisher"):
            sr_score = _clamp((shrunk_sr - 100) / 100 * 55, 0, 55)
            rpm_score = _clamp(shrunk_rpm / 35 * 45, 0, 45)
            return sr_score + rpm_score

        if role == "Anchor":
            avg = float(player.get("average", 0) or 0)
            avg_score = _clamp(avg / 45 * 50, 0, 50)
            rpm_score = _clamp(shrunk_rpm / 30 * 50, 0, 50)
            return avg_score + rpm_score

        if role in ("Fast Bowling AR", "All Rounder"):
            bat = _clamp(shrunk_rpm / 25 * 50, 0, 50)
            bowl = _clamp((12 - shrunk_econ) / 4 * 25 + shrunk_wpm * 25, 0, 50)
            return bat + bowl

        # Default batter / player
        bat = _clamp(shrunk_rpm / 30 * 40 + (shrunk_sr - 100) / 80 * 30, 0, 70)
        form_part = _clamp(shrunk_form, 0, 100) * 0.3
        return bat + form_part

    def cohort_z_score(self, raw: float, role: str) -> float:
        """Z-score vs players with similar primary role bucket."""
        role_bucket = {
            "Death Bowler": "bowler",
            "Powerplay Bowler": "bowler",
            "Bowler": "bowler",
            "Wrist Spinner": "bowler",
            "Left Arm Pacer": "bowler",
            "Finisher": "batter",
            "Anchor": "batter",
            "WK Finisher": "batter",
            "Batter": "batter",
            "Fast Bowling AR": "ar",
            "All Rounder": "ar",
            "Player": "all",
        }.get(role, "all")

        if role_bucket == "bowler":
            filt = "total_wickets >= 10"
        elif role_bucket == "batter":
            filt = "total_runs >= 150"
        elif role_bucket == "ar":
            filt = "total_runs >= 100 AND total_wickets >= 3"
        else:
            filt = "matches_played >= 3"

        rows = self.conn.execute(
            f"""
            SELECT matches_played, total_runs, total_wickets, strike_rate,
                   economy_rate, form_rating, last_10_matches_sr, last_10_matches_economy
            FROM player_auction_stats
            WHERE {filt}
            """
        ).fetchall()

        scorer = PerformanceScorer.__new__(PerformanceScorer)
        scorer.conn = self.conn
        scorer._league_priors = self._league_priors

        scores = []
        for r in rows:
            p = {
                "matches_played": r[0],
                "total_runs": r[1],
                "total_wickets": r[2],
                "strike_rate": r[3],
                "economy_rate": r[4],
                "form_rating": r[5],
                "last_10_matches_sr": r[6],
                "last_10_matches_economy": r[7],
            }
            scores.append(scorer.raw_score(p, role))

        if len(scores) < 5:
            return 0.0
        mean = sum(scores) / len(scores)
        std = math.sqrt(sum((s - mean) ** 2 for s in scores) / len(scores)) or 1.0
        return (raw - mean) / std


# ── Age curve ──────────────────────────────────────────────────────────────────

def is_elite_player(player: Dict) -> bool:
    """Proven IPL contributors — avoid underpricing established stars."""
    matches = int(player.get("matches_played") or 0)
    runs = int(player.get("total_runs") or 0)
    wickets = int(player.get("total_wickets") or 0)
    if matches < 25:
        return False
    if wickets >= 80:
        return True
    if runs >= 1200:
        return True
    if wickets >= 50 and float(player.get("economy_rate", 10) or 10) <= 8.5:
        return True
    return False


def age_multiplier(age: int) -> Tuple[float, bool]:
    if age < 23:
        return 1.12, True
    if age < 30:
        return 1.00, False
    return 0.88, False


# ── Verdict engine ─────────────────────────────────────────────────────────────

def derive_verdict(
    csk_fit: float,
    median: float,
    confidence: int,
    risk: Dict,
    metadata: Dict,
    historical_price: Optional[float],
) -> str:
    age = metadata.get("age", 27)
    surplus = 0.0
    if historical_price and historical_price > 0:
        surplus = historical_price - median

    if csk_fit >= 78 and confidence >= 60:
        return "🔥 MUST BUY"
    if csk_fit >= 65 and confidence >= 50 and median <= 8:
        return "🎯 Strong Target"
    if csk_fit >= 55 and median <= 4 and confidence >= 40:
        return "💎 Value Pick"
    if age < 23 and confidence < 45 and csk_fit >= 48:
        return "🌱 Development Buy"
    if csk_fit >= 50 and confidence < 40:
        return "📋 Monitor (High Uncertainty)"
    if surplus > 2 and csk_fit < 55:
        return "⚠️ Overpriced vs Market"
    if csk_fit >= 45:
        return "📋 Monitor"
    return "❌ Avoid"


# ── Main franchise valuation engine ───────────────────────────────────────────

@dataclass
class ValuationResult:
    player_name: str
    role: str
    role_detail: str
    age: int
    injury_risk: str
    form_score: float
    form_score_raw: float
    form_role_bucket: str
    csk_fit_score: float
    csk_fit_reasons: List[str]
    estimated_value: float
    floor_price: float
    ceiling_price: float
    auction_verdict: str
    market_value: Dict[str, float]
    confidence: int
    volatility: str
    scarcity_bonus: float
    age_upside: bool
    experience_factor: float
    career_runs: int
    career_wickets: int
    career_sr: float
    career_econ: float
    last_10_runs: int
    last_10_wickets: int
    last_10_sr: float
    last_10_econ: float
    matches_played: int
    z_score: float = 0.0
    historical_auction_price: Optional[float] = None
    market_premium: float = 1.0
    pricing_method: str = "rules"
    rule_based_value: Optional[float] = None
    ml_predicted_value: Optional[float] = None
    ml_confidence: Optional[int] = None
    observability: Optional[Dict] = None

    def to_api_dict(self, *, include_observability: bool = False) -> Dict:
        d = {
            "player_name": self.player_name,
            "role": self.role,
            "role_detail": self.role_detail,
            "age": self.age,
            "injury_risk": self.injury_risk,
            "form_score": self.form_score,
            "form_score_raw": self.form_score_raw,
            "form_role_bucket": self.form_role_bucket,
            "csk_fit_score": self.csk_fit_score,
            "csk_fit_reasons": self.csk_fit_reasons,
            "estimated_value": self.estimated_value,
            "floor_price": self.floor_price,
            "ceiling_price": self.ceiling_price,
            "auction_verdict": self.auction_verdict,
            "market_value": self.market_value,
            "confidence": self.confidence,
            "volatility": self.volatility,
            "scarcity_bonus": self.scarcity_bonus,
            "market_premium": self.market_premium,
            "age_upside": self.age_upside,
            "experience_factor": self.experience_factor,
            "career_runs": self.career_runs,
            "career_wickets": self.career_wickets,
            "career_sr": self.career_sr,
            "career_econ": self.career_econ,
            "last_10_runs": self.last_10_runs,
            "last_10_wickets": self.last_10_wickets,
            "last_10_sr": self.last_10_sr,
            "last_10_econ": self.last_10_econ,
            "matches_played": self.matches_played,
            "z_score": round(self.z_score, 3),
        }
        if self.historical_auction_price is not None:
            d["historical_auction_price"] = self.historical_auction_price
        if self.ml_predicted_value is not None:
            d["ml_predicted_value"] = self.ml_predicted_value
            d["rule_based_value"] = self.rule_based_value
            d["ml_confidence"] = self.ml_confidence
            d["pricing_method"] = self.pricing_method
        if include_observability and self.observability:
            d["observability"] = self.observability
        return d


class FranchiseValuationEngine:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.market_model = AuctionMarketModel(conn)
        self.role_engine = RoleInferenceEngine()
        self.risk_engine = RiskEngine()
        self.csk_engine = CSKFitEngine()
        self.scorer = PerformanceScorer(conn)
        from form_normalizer import FormNormalizer

        self.form_normalizer = FormNormalizer(conn)
        try:
            from ml_valuation import MLValuationModel
            self.ml_model = MLValuationModel()
        except ImportError:
            self.ml_model = None

    def valuate(self, player: Dict, metadata: Dict) -> ValuationResult:
        role, role_detail = self.role_engine.infer(player)
        risk = self.risk_engine.analyze(player)
        csk_fit, reasons = self.csk_engine.score(player, role, metadata)

        market = self.market_model.get_market_distribution()
        # Use IPL scarcity table for pricing (market premium alone underprices death bowlers)
        scarcity = ROLE_SCARCITY.get(role, 1.0)
        market_premium = self.market_model.get_role_premium(role)

        raw = self.scorer.raw_score(player, role)
        z = self.scorer.cohort_z_score(raw, role)
        matches = player.get("matches_played", 0) or 0
        exp_factor = risk["experience_factor"]
        elite = is_elite_player(player)
        hist = self.market_model.get_historical_price(player.get("player_name", ""))
        hist_weight = 0.0
        w_ml = 0.0

        # Dampen z-score for small samples; allow more signal for elite players
        if elite:
            effective_z = _clamp(z, -2.5, 2.5)
        else:
            effective_z = _clamp(z * exp_factor, -2.0, 2.0)
            if matches < 25:
                effective_z = _clamp(effective_z, -1.0, 1.0)
            if matches < 20:
                effective_z = _clamp(effective_z, -0.6, 0.6)

        base_price = market["mean"] + effective_z * market["std"]

        age = int(metadata.get("age", 27))
        age_mult, age_upside = age_multiplier(age)
        if matches < 25 or risk["confidence"] < 50:
            age_mult = 1.0
            age_upside = age < 23 and matches >= 20

        form_raw = float(player.get("form_rating", 50) or 50)
        form_norm, form_bucket = self.form_normalizer.normalize(player, role, form_raw)
        form_adj = 0.95 + (form_norm / 100) * 0.10 * exp_factor
        csk_adj = 0.90 + (csk_fit / 100) * 0.20

        median = base_price * scarcity * age_mult * form_adj * csk_adj

        prior_anchor = market["p50"]
        shrink_weight = PRIOR_WEIGHT * (1.15 - exp_factor)
        median = (median * matches + prior_anchor * shrink_weight) / (matches + shrink_weight)

        # Young uncapped: hard cap toward market p50 (~₹2 Cr for Vaibhav-type profiles)
        if not hist and not elite and matches < 20:
            uncapped_cap = market["p50"] * scarcity * (1.15 + 0.35 * exp_factor)
            median = min(median, uncapped_cap)
        elif not hist and not elite and matches < 30:
            uncapped_cap = market["p75"] * scarcity * (0.80 + 0.12 * exp_factor)
            median = min(median, uncapped_cap)

        # Elite floor — death bowlers / anchors with proven IPL body of work
        if elite and not hist:
            elite_floor = market["p75"] * scarcity
            if role in ("Death Bowler", "Finisher", "Anchor"):
                elite_floor = max(elite_floor, market["p90"] * 0.65 * scarcity)
            if role == "Death Bowler" and (player.get("total_wickets", 0) or 0) >= 100:
                elite_floor = max(elite_floor, market["p90"] * 0.85)
            median = max(median, elite_floor)

        # Anchor to real auction clears when available
        if hist and hist > 0:
            hist_weight = _clamp(0.30 + 0.40 * exp_factor + (0.15 if elite else 0), 0.35, 0.75)
            median = median * (1 - hist_weight) + hist * hist_weight

        rule_median_pre_floor = median
        if median < PRICE_MIN:
            median = PRICE_MIN
        rule_median = median
        ml_price: Optional[float] = None
        ml_conf = 0
        pricing_method = "rules"

        if self.ml_model and self.ml_model.is_ready():
            ml_price, ml_conf = self.ml_model.predict(player, role)
            if ml_conf >= 25:
                from model_observability import ml_model_health

                ml_trusted = ml_model_health(getattr(self.ml_model, "metrics", None)).get(
                    "trusted", False
                )
                # Learned auction prices blended with rule engine (guardrails stay on rule side)
                w_ml = 0.55 if matches >= 30 and not elite else 0.40
                if matches < 20:
                    w_ml = 0.22  # uncapped: mostly rules + shrinkage
                if hist:
                    w_ml *= 0.45
                if not ml_trusted:
                    # Negative R² / thin training — keep ML in observability only, not pricing
                    w_ml = 0.0
                if w_ml > 0:
                    median = (1 - w_ml) * rule_median + w_ml * ml_price
                    pricing_method = "ml_rules_blend"
                    if not hist and not elite and matches < 20:
                        uncapped_cap = market["p50"] * scarcity * (1.15 + 0.35 * exp_factor)
                        median = min(median, uncapped_cap)

        median = round(_clamp(median, PRICE_MIN, PRICE_MAX), 2)

        vol = risk["volatility_multiplier"]
        spread = market["std"] * vol * (1.4 - exp_factor * 0.4)

        p10 = round(_clamp(median - 1.28 * spread, PRICE_MIN, PRICE_MAX), 2)
        p25 = round(_clamp(median - 0.67 * spread, PRICE_MIN, PRICE_MAX), 2)
        p75 = round(_clamp(median + 0.67 * spread, PRICE_MIN, PRICE_MAX), 2)
        p90 = round(_clamp(median + 1.28 * spread, PRICE_MIN, PRICE_MAX), 2)

        verdict = derive_verdict(csk_fit, median, risk["confidence"], risk, metadata, hist)

        ml_metrics = getattr(self.ml_model, "metrics", None) if self.ml_model else None
        from model_observability import build_valuation_observability

        observability = build_valuation_observability(
            player=player,
            metadata=metadata,
            role=role,
            role_detail=role_detail,
            risk=risk,
            market=market,
            scarcity=scarcity,
            market_premium=market_premium,
            effective_z=effective_z,
            raw_z=z,
            elite=elite,
            age_mult=age_mult,
            age_upside=age_upside,
            form_norm=form_norm,
            form_bucket=form_bucket,
            form_adj=form_adj,
            csk_fit=csk_fit,
            csk_adj=csk_adj,
            rule_median=rule_median,
            rule_median_pre_floor=rule_median_pre_floor,
            ml_price=ml_price,
            ml_conf=ml_conf,
            w_ml=w_ml,
            pricing_method=pricing_method,
            hist=hist,
            hist_weight=hist_weight,
            shrink_weight=shrink_weight,
            prior_anchor=prior_anchor,
            median=median,
            verdict=verdict,
            ml_metrics=ml_metrics,
        )

        return ValuationResult(
            player_name=player.get("player_name", ""),
            role=role,
            role_detail=role_detail,
            age=age,
            injury_risk=metadata.get("injury_risk", "Medium"),
            form_score=round(form_norm, 1),
            form_score_raw=round(form_raw, 1),
            form_role_bucket=form_bucket,
            csk_fit_score=csk_fit,
            csk_fit_reasons=reasons,
            estimated_value=median,
            floor_price=p10,
            ceiling_price=p90,
            auction_verdict=verdict,
            market_value={"p10": p10, "p25": p25, "p50": median, "p75": p75, "p90": p90},
            confidence=risk["confidence"],
            volatility=risk["volatility"],
            scarcity_bonus=round(scarcity, 3),
            market_premium=round(market_premium, 3),
            age_upside=age_upside,
            experience_factor=exp_factor,
            career_runs=player.get("total_runs", 0) or 0,
            career_wickets=player.get("total_wickets", 0) or 0,
            career_sr=round(float(player.get("strike_rate", 0) or 0), 1),
            career_econ=round(float(player.get("economy_rate", 0) or 0), 1),
            last_10_runs=int(player.get("last_10_matches_runs", 0) or 0),
            last_10_wickets=int(player.get("last_10_matches_wickets", 0) or 0),
            last_10_sr=round(float(player.get("last_10_matches_sr", 0) or 0), 1),
            last_10_econ=round(float(player.get("last_10_matches_economy", 0) or 0), 1),
            matches_played=player.get("matches_played", 0) or 0,
            z_score=effective_z,
            historical_auction_price=hist,
            pricing_method=pricing_method,
            rule_based_value=round(rule_median, 2),
            ml_predicted_value=ml_price,
            ml_confidence=ml_conf if ml_price else None,
            observability=observability,
        )
