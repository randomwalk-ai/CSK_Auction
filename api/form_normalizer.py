"""
Role-wise normalization for form_rating (0–100 raw composite).

Raw form favours all-rounders/bowlers; this maps each player to a percentile
within their role bucket (batter / bowler / all_rounder / other).
"""

from __future__ import annotations

import sqlite3
from typing import Dict, List, Tuple


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))

BOWLER_ROLES = frozenset(
    {"Death Bowler", "Powerplay Bowler", "Bowler", "Wrist Spinner"}
)
AR_ROLES = frozenset({"All Rounder", "Fast Bowling AR"})
BATTER_ROLES = frozenset({"Batter", "Finisher", "Anchor", "WK Finisher"})

# Theoretical caps when cohort too small for percentile
ROLE_FORM_CAPS = {
    "batter": 55.0,
    "bowler": 65.0,
    "all_rounder": 100.0,
    "other": 85.0,
}


def role_bucket(specialist_role: str, player: Dict) -> str:
    if specialist_role in BOWLER_ROLES:
        return "bowler"
    if specialist_role in AR_ROLES:
        return "all_rounder"
    if specialist_role in BATTER_ROLES:
        return "batter"
    wickets = player.get("total_wickets", 0) or 0
    runs = player.get("total_runs", 0) or 0
    if wickets >= 15 and runs < 200:
        return "bowler"
    if runs >= 200 and wickets >= 5:
        return "all_rounder"
    if runs >= 150:
        return "batter"
    return "other"


class FormNormalizer:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._role_engine = None
        self._distributions: Dict[str, List[float]] | None = None

    def _get_role_engine(self):
        if self._role_engine is None:
            from valuation_engine import RoleInferenceEngine

            self._role_engine = RoleInferenceEngine()
        return self._role_engine

    def _load_player_rows(self) -> List[Dict]:
        conn = self.conn
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM player_auction_stats
            WHERE form_rating IS NOT NULL
            ORDER BY
                player_name,
                CASE WHEN LOWER(competition) = 'ipl' THEN 0 ELSE 1 END,
                matches_played DESC
            """
        ).fetchall()
        seen = set()
        out: List[Dict] = []
        for row in rows:
            name = row["player_name"]
            if name in seen:
                continue
            seen.add(name)
            out.append({k: row[k] for k in row.keys()})
        return out

    def _ensure_distributions(self) -> None:
        if self._distributions is not None:
            return
        buckets: Dict[str, List[float]] = {
            "batter": [],
            "bowler": [],
            "all_rounder": [],
            "other": [],
        }
        for player in self._load_player_rows():
            raw = float(player.get("form_rating") or 0)
            if raw <= 0:
                continue
            role, _ = self._get_role_engine().infer(player)
            bucket = role_bucket(role, player)
            buckets[bucket].append(raw)
        self._distributions = buckets

    def normalize(
        self, player: Dict, specialist_role: str, raw_form: float
    ) -> Tuple[float, str]:
        self._ensure_distributions()
        bucket = role_bucket(specialist_role, player)
        peers = self._distributions.get(bucket) or []

        if len(peers) < 8:
            cap = ROLE_FORM_CAPS.get(bucket, 85.0)
            scaled = _clamp(raw_form / cap * 100.0, 0.0, 100.0)
            return round(scaled, 1), bucket

        below = sum(1 for p in peers if p <= raw_form)
        percentile = 100.0 * below / len(peers)
        return round(_clamp(percentile, 0.0, 100.0), 1), bucket
