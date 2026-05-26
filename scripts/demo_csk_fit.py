#!/usr/bin/env python3
"""
Print CSK fit score breakdown + role-normalized form for two players.

Usage:
  python scripts/demo_csk_fit.py
  python scripts/demo_csk_fit.py "Ruturaj Gaikwad" "Rahul Chahar"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))

import sqlite3

from form_normalizer import FormNormalizer
from player_loader import get_player_stats
from player_metadata import fetch_metadata
from valuation_engine import CSKFitEngine, RoleInferenceEngine

DB_PATH = ROOT / "auction_data.db"


def print_player(conn, name: str) -> None:
    player = get_player_stats(conn, name)
    if not player:
        print(f"\n❌ Player not found: {name}\n")
        return

    metadata = fetch_metadata(player["player_name"], search_name=name)
    role_engine = RoleInferenceEngine()
    csk_engine = CSKFitEngine()
    normalizer = FormNormalizer(conn)

    role, role_detail = role_engine.infer(player)
    raw_form = float(player.get("form_rating") or 0)
    norm_form, bucket = normalizer.normalize(player, role, raw_form)
    fit = csk_engine.score_with_breakdown(player, role, metadata)

    print("\n" + "=" * 72)
    print(f"  {player['player_name']}")
    print(f"  Role: {role} — {role_detail}")
    print("=" * 72)
    print(f"  Last 10: SR {player.get('last_10_matches_sr') or '—'} | "
          f"Econ {player.get('last_10_matches_economy') or '—'} | "
          f"Runs {player.get('last_10_matches_runs') or 0} | "
          f"Wkts {player.get('last_10_matches_wickets') or 0}")
    print(f"  Form (raw composite):     {raw_form:.1f} / 100")
    print(f"  Form (role-normalized):   {norm_form:.1f} / 100  [{bucket} cohort]")
    print(f"  CSK fit (final):          {fit['final_score']:.1f} / 100")
    print("\n  CSK fit breakdown:")
    print(f"  {'Step':<28} {'Δ':>6}  {'Running':>8}")
    print(f"  {'-'*28} {'-'*6}  {'-'*8}")
    for row in fit["breakdown"]:
        if row.get("step") == "adj_form":
            print(f"  {'(adj form detail)':<28} {'':>6}  {row.get('detail', '')}")
            continue
        delta = row.get("delta", 0)
        delta_s = f"{delta:+.0f}" if delta else "—"
        print(f"  {row['label']:<28} {delta_s:>6}  {row['running_total']:>8.1f}")
        if row.get("reason"):
            print(f"      → {row['reason']}")

    if fit["reasons"]:
        print("\n  Top reasons:")
        for r in fit["reasons"]:
            print(f"    • {r}")
    print()


def main() -> None:
    names = sys.argv[1:] if len(sys.argv) > 1 else ["Ruturaj Gaikwad", "Rahul Chahar"]
    if len(names) != 2:
        print("Provide exactly 2 player names, or use defaults.")
        names = names[:2] if len(names) >= 2 else ["Ruturaj Gaikwad", "Rahul Chahar"]

    conn = sqlite3.connect(DB_PATH)
    for name in names:
        print_player(conn, name)

    print("Tip: full JSON breakdown available via score_with_breakdown() in valuation_engine.py")


if __name__ == "__main__":
    main()
