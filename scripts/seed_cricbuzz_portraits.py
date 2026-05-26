#!/usr/bin/env python3
"""
Fetch Cricbuzz portraits for IPL 2026 auction pool players.

Uses cricbuzz_player_id from auction_prices_full (seed via seed_missing_cricbuzz_ids.py).
Reuses player_portrait_store URL patterns and DB cache — no extra packages.

Usage (from auction-data-pipeline/):
  python3 scripts/seed_missing_cricbuzz_ids.py --apply
  python3 scripts/seed_cricbuzz_portraits.py --dry-run --player "Virat Kohli"
  python3 scripts/seed_cricbuzz_portraits.py --apply --delay 0.5
  python3 scripts/player_portraits.py audit
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
API_DIR = ROOT / "api"

sys.path.insert(0, str(API_DIR))

from env_loader import load_project_dotenv  # noqa: E402

load_project_dotenv()

from player_portrait_store import (  # noqa: E402
    _connect,
    _lookup_cricbuzz_id,
    _normalize_key,
    collect_portrait_names,
    fetch_cricbuzz_portrait_only,
)


def _load_players(limit: int = 0, only: str = "") -> list[str]:
    if only:
        return [only.strip()]
    names = collect_portrait_names()
    if limit > 0:
        return names[:limit]
    return names


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed Cricbuzz portraits for auction pool (ID-based CDN)",
    )
    parser.add_argument("--apply", action="store_true", help="Write to DB (default is dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="Check IDs only, no downloads")
    parser.add_argument("--force", action="store_true", help="Re-fetch even if cricbuzz cached")
    parser.add_argument("--delay", type=float, default=0.5, help="Seconds between players")
    parser.add_argument("--limit", type=int, default=0, help="Max players (0 = all)")
    parser.add_argument("--player", type=str, default="", help="Single player name")
    args = parser.parse_args()

    dry = not args.apply or args.dry_run
    players = _load_players(args.limit, args.player)
    if not players:
        print("No players to process.")
        return 1

    print(
        f"Players: {len(players)}  mode: {'dry-run' if dry else 'apply'}  "
        f"delay: {args.delay}s\n"
    )

    ok = miss = no_id = skip = 0
    conn = _connect()

    try:
        with httpx.Client(follow_redirects=True, trust_env=False) as client:
            for idx, name in enumerate(players, start=1):
                key = _normalize_key(name)
                pid = _lookup_cricbuzz_id(conn, name)

                if not pid:
                    no_id += 1
                    print(f"  [{idx}] NO_ID  {name}  (run seed_missing_cricbuzz_ids.py --apply)")
                    continue

                if dry:
                    print(f"  [{idx}] OK     {name}  cricbuzz_id={pid}")
                    ok += 1
                    continue

                if not args.force:
                    row = conn.execute(
                        "SELECT source FROM player_portraits WHERE player_key = ?",
                        (key,),
                    ).fetchone()
                    if row and str(row[0]) == "cricbuzz":
                        skip += 1
                        if idx <= 3 or idx % 25 == 0:
                            print(f"  [{idx}] skip (cached) {name}")
                        continue

                hit = fetch_cricbuzz_portrait_only(
                    conn, client, name, force=args.force,
                )
                if hit:
                    ok += 1
                    print(f"  [{idx}] OK     {name}  id={pid}  {len(hit[0])} bytes")
                else:
                    miss += 1
                    print(f"  [{idx}] MISS   {name}  id={pid}  (CDN download failed)")

                if args.delay > 0 and idx < len(players):
                    time.sleep(args.delay)
    finally:
        conn.close()

    print(
        f"\nDone: ok={ok}  miss={miss}  no_id={no_id}  skipped={skip}  total={len(players)}"
    )
    if dry:
        print("Re-run with --apply to download and store portraits.")
    else:
        print("Hard-refresh the dashboard (Cmd+Shift+R).")
        print("Run: python3 scripts/player_portraits.py audit")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
