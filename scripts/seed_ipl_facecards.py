#!/usr/bin/env python3
"""
Seed IPL official face-card URLs for the auction pool (no image bytes).

Based on iplt20.com squad scrape + documents.iplt20.com headshots.

Usage (from auction-data-pipeline/):
  python3 scripts/seed_ipl_facecards.py --dry-run --limit 10
  python3 scripts/seed_ipl_facecards.py --apply
  python3 scripts/seed_ipl_facecards.py --apply --player "Virat Kohli"
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
API_DIR = ROOT / "api"

sys.path.insert(0, str(API_DIR))

from player_portrait_store import (  # noqa: E402
    DB_PATH,
    _connect,
    _normalize_key,
    collect_portrait_names,
)
from ipl_facecards import (  # noqa: E402
    build_squad_index,
    ensure_ipl_facecard_schema,
    find_working_facecard_url,
    lookup_in_squad_index,
    save_facecard_row,
)


def _load_players(limit: int, only: str) -> list[str]:
    if only:
        return [only.strip()]
    names = collect_portrait_names()
    return names[:limit] if limit > 0 else names


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed IPL face-card URLs (iplt20.com)")
    parser.add_argument("--apply", action="store_true", help="Write URLs to DB")
    parser.add_argument("--dry-run", action="store_true", help="Resolve only, no DB writes")
    parser.add_argument("--delay", type=float, default=0.2, help="Seconds between HEAD checks")
    parser.add_argument("--limit", type=int, default=0, help="Max players (0 = all)")
    parser.add_argument("--player", type=str, default="", help="Single player name")
    args = parser.parse_args()

    dry = not args.apply or args.dry_run
    players = _load_players(args.limit, args.player)
    if not players:
        print("No players to process.")
        return 1

    print(f"Database: {DB_PATH}")
    print(
        f"Players: {len(players)}  mode: {'dry-run' if dry else 'apply'}\n"
        "Building squad index from iplt20.com (10 teams)..."
    )

    conn = _connect()
    ensure_ipl_facecard_schema(conn)
    ok = miss = 0

    try:
        with httpx.Client(follow_redirects=True, trust_env=False) as client:
            index = build_squad_index(client)
            print(f"Squad index: {len(index)} players\n")

            for idx, name in enumerate(players, start=1):
                match = lookup_in_squad_index(name, index)
                if not match:
                    miss += 1
                    print(f"  [{idx}] MISS   {name}  (not on IPL squad pages)")
                    continue

                url, year = find_working_facecard_url(client, match["id"])
                if not url:
                    miss += 1
                    print(
                        f"  [{idx}] MISS   {name}  id={match['id']} "
                        f"(no 200 on headshot years)"
                    )
                    continue

                ok += 1
                print(
                    f"  [{idx}] OK     {name}  -> {match['display_name']}  "
                    f"id={match['id']}  y={year}"
                )

                if not dry:
                    save_facecard_row(
                        conn,
                        player_key=_normalize_key(name),
                        display_name=name,
                        ipl_player_id=match["id"],
                        facecard_url=url,
                        facecard_year=year,
                        matched_ipl_name=match["display_name"],
                        team_slug=match["team"],
                    )

                if args.delay > 0 and idx < len(players):
                    time.sleep(args.delay)
    finally:
        conn.close()

    print(f"\nDone: ok={ok}  miss={miss}  total={len(players)}")
    if dry:
        print("Re-run with --apply to save facecard_url rows.")
    else:
        print("Hard-refresh the dashboard (Cmd+Shift+R).")
        print("Arena loads facecard_url from /api/players/auction-pool.")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
