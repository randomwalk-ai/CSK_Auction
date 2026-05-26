#!/usr/bin/env python3
"""
Fetch uniform ESPN Cricinfo portraits for IPL 2026 auction pool players.

Stores:
  - player_espn_cricinfo (espn id + uniform CDN URL per player_key)
  - player_portraits (image bytes, source=espncricinfo)
  - auction_prices_full.espn_cricinfo_id (when name matches)

Usage (from auction-data-pipeline/):
  python3 scripts/seed_espn_portraits.py --dry-run --limit 5
  python3 scripts/seed_espn_portraits.py --apply --delay 1.2
  python3 scripts/seed_espn_portraits.py --apply --player "Virat Kohli"
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
AUCTION_DB = ROOT / "auction_data.db"

sys.path.insert(0, str(API_DIR))

from env_loader import load_project_dotenv  # noqa: E402

load_project_dotenv()

from espn_cricinfo import (  # noqa: E402
    ensure_espn_schema,
    fetch_and_store_espn_portrait,
    lookup_espn_mapping,
    resolve_espn_player,
)
from player_portrait_store import (  # noqa: E402
    _clear_portrait_entry,
    _connect,
    _normalize_key,
    collect_portrait_names,
)


def _load_players(limit: int = 0, only: str = "") -> list[str]:
    if only:
        return [only.strip()]
    names = collect_portrait_names()
    if limit > 0:
        return names[:limit]
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed ESPN Cricinfo portraits for auction pool")
    parser.add_argument("--apply", action="store_true", help="Write to DB (default is dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="Resolve URLs only, no DB writes")
    parser.add_argument("--force", action="store_true", help="Re-fetch even if espncricinfo cached")
    parser.add_argument("--delay", type=float, default=1.2, help="Seconds between players")
    parser.add_argument("--limit", type=int, default=0, help="Max players (0 = all)")
    parser.add_argument("--player", type=str, default="", help="Single player name")
    args = parser.parse_args()

    dry = not args.apply or args.dry_run
    players = _load_players(args.limit, args.player)
    if not players:
        print("No players to process.")
        return 1

    print(f"Players: {len(players)}  mode: {'dry-run' if dry else 'apply'}  delay: {args.delay}s\n")

    ok = miss = skip = 0
    conn = _connect()
    ensure_espn_schema(conn)

    try:
        with httpx.Client(follow_redirects=True, trust_env=False) as client:
            for idx, name in enumerate(players, start=1):
                key = _normalize_key(name)
                if not args.force:
                    mapping = lookup_espn_mapping(conn, key)
                    if mapping and not dry:
                        cached = conn.execute(
                            "SELECT source FROM player_portraits WHERE player_key = ?",
                            (key,),
                        ).fetchone()
                        if cached and str(cached[0]) == "espncricinfo":
                            skip += 1
                            if idx <= 3 or idx % 25 == 0:
                                print(f"  [{idx}] skip (cached) {name}")
                            continue

                resolved = resolve_espn_player(client, name)
                if not resolved:
                    miss += 1
                    print(f"  [{idx}] MISS  {name}")
                    continue

                print(
                    f"  [{idx}] OK    {name}  id={resolved['espn_player_id']}  "
                    f"{resolved['uniform_image_url'][:72]}…"
                )

                if dry:
                    ok += 1
                    continue

                if args.force:
                    _clear_portrait_entry(conn, key)

                hit = fetch_and_store_espn_portrait(
                    conn, client, name, force=args.force,
                )
                if hit:
                    ok += 1
                else:
                    miss += 1
                    print(f"       (download failed) {name}")

                if args.delay > 0 and idx < len(players):
                    time.sleep(args.delay)
    finally:
        conn.close()

    print(f"\nDone: ok={ok}  miss={miss}  skipped={skip}  total={len(players)}")
    if dry:
        print("Re-run with --apply to write portraits to the database.")
    else:
        print("Hard-refresh the dashboard (Cmd+Shift+R).")
        print("Run: python3 scripts/player_portraits.py audit")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
