#!/usr/bin/env python3
"""
Sample valuations with full observability traces — no dashboard UI.

  python scripts/audit_valuation_observability.py
  python scripts/audit_valuation_observability.py --players "Ravindra Jadeja" "Matheesha Pathirana"
  python scripts/audit_valuation_observability.py --limit 20 --out reports/obs_audit.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))

from player_loader import get_player_stats
from player_metadata import fetch_metadata as get_player_metadata
from valuation_engine import FranchiseValuationEngine


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit valuation model observability")
    parser.add_argument("--players", nargs="*", help="Specific player names")
    parser.add_argument("--limit", type=int, default=12, help="Random sample size if no --players")
    parser.add_argument("--out", type=Path, help="Write JSON report to file")
    args = parser.parse_args()

    import os

    db = Path(os.getenv("DB_PATH", str(ROOT / "auction_data.db")))
    if not db.exists():
        print(f"Database not found: {db}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    names = args.players
    if not names:
        rows = conn.execute(
            """
            SELECT player_name FROM player_auction_stats
            WHERE matches_played >= 10
            ORDER BY RANDOM() LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
        names = [r["player_name"] for r in rows]

    engine = FranchiseValuationEngine(conn)
    report = {"sample_n": len(names), "players": []}
    flag_counts: dict[str, int] = {}

    for name in names:
        player = get_player_stats(conn, name)
        if not player:
            report["players"].append({"player_name": name, "error": "not_found"})
            continue
        meta = get_player_metadata(player["player_name"], search_name=name)
        result = engine.valuate(player, meta)
        obs = result.observability or {}
        for f in obs.get("data_quality", {}).get("flags") or []:
            flag_counts[f.get("code", "?")] = flag_counts.get(f.get("code", "?"), 0) + 1
        report["players"].append(
            {
                "player_name": result.player_name,
                "median_cr": result.estimated_value,
                "pricing_method": result.pricing_method,
                "confidence_pct": result.confidence,
                "flags": [f.get("code") for f in obs.get("data_quality", {}).get("flags") or []],
                "top_drivers": obs.get("top_drivers"),
                "ml_trusted": (obs.get("ml_model") or {}).get("trusted"),
                "observability": obs,
            }
        )

    report["flag_summary"] = flag_counts
    at_floor = sum(
        1
        for p in report["players"]
        if "priced_at_floor" in (p.get("flags") or [])
    )
    if at_floor:
        report["notes"] = {
            "priced_at_floor_count": at_floor,
            "hint": "Weak z-score vs high match count can hit ₹0.20 Cr floor — check rule_median_pre_floor_cr",
        }
    text = json.dumps(report, indent=2, default=str)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
        print(f"Wrote {args.out}")
    else:
        print(text)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
