#!/usr/bin/env python3
"""
Fetch ESPN Cricinfo portraits only for auction-pool players without an IPL face card.

Stores:
  - player_espn_cricinfo (espn id + uniform CDN URL per player_key)
  - player_portraits (image bytes, source=espncricinfo)

Usage (from auction-data-pipeline/):
  python3 scripts/seed_espn_portraits.py --dry-run --limit 5
  python3 scripts/seed_espn_portraits.py --apply --delay 1.2
  python3 scripts/seed_espn_portraits.py --apply --player "Josh Hazlewood"
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API_DIR = ROOT / "api"
AUCTION_DB = ROOT / "auction_data.db"

sys.path.insert(0, str(API_DIR))

from env_loader import load_project_dotenv  # noqa: E402

load_project_dotenv()

from espn_cricinfo import (  # noqa: E402
    ensure_espn_schema,
    espn_connectivity_self_check,
    espn_http_client,
    fetch_and_store_espn_portrait,
    lookup_espn_mapping,
    resolve_espn_player,
    warm_espn_roster_index,
)
from ipl_facecards import has_ipl_facecard  # noqa: E402
from player_portrait_store import (  # noqa: E402
    _clear_portrait_entry,
    _connect,
    _load_canonical_index,
    _normalize_key,
    _repair_pdf_name,
    collect_portrait_names,
)


def _load_players(limit: int = 0, only: str = "", include_facecard: bool = False) -> list[str]:
    if only:
        names = [only.strip()]
    else:
        names = collect_portrait_names()
    conn = _connect()
    try:
        if not include_facecard:
            names = [n for n in names if n and not has_ipl_facecard(conn, n)]
    finally:
        conn.close()
    if limit > 0:
        return names[:limit]
    return names


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed ESPN portraits for players without an IPL face card",
    )
    parser.add_argument("--apply", action="store_true", help="Write to DB (default is dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="Resolve URLs only, no DB writes")
    parser.add_argument("--force", action="store_true", help="Re-fetch even if espncricinfo cached")
    parser.add_argument(
        "--include-facecard",
        action="store_true",
        help="Also process players who already have IPL face cards (not recommended)",
    )
    parser.add_argument("--delay", type=float, default=1.2, help="Seconds between players")
    parser.add_argument("--limit", type=int, default=0, help="Max players (0 = all)")
    parser.add_argument("--player", type=str, default="", help="Single player name")
    parser.add_argument("--verbose", action="store_true", help="Log why each player missed")
    parser.add_argument(
        "--skip-roster-warm",
        action="store_true",
        help="Skip IPL squad index build (slower per-player search)",
    )
    args = parser.parse_args()

    dry = not args.apply or args.dry_run
    players = _load_players(args.limit, args.player, include_facecard=args.include_facecard)
    if not players:
        print("No players without IPL face cards to process.")
        print("Run: python3 scripts/seed_ipl_facecards.py --apply  first, or use --include-facecard.")
        return 1

    print(
        f"Players (no face card): {len(players)}  mode: {'dry-run' if dry else 'apply'}  "
        f"delay: {args.delay}s\n"
    )

    ok = miss = skip = 0
    conn = _connect()
    ensure_espn_schema(conn)
    canonical_index = _load_canonical_index(conn)

    try:
        with espn_http_client() as client:
            issues = espn_connectivity_self_check(client)
            if issues:
                print("ESPN connectivity warnings:")
                for msg in issues:
                    print(f"  - {msg}")
                print(
                    "  Tip: export ESPN_COOKIES from Chrome (document.cookie on espncricinfo.com), "
                    "or ESPN_USE_PLAYWRIGHT=1; unset HTTP_PROXY or set ESPN_HTTP_TRUST_ENV=1\n"
                )

            if not args.skip_roster_warm:
                n_roster = warm_espn_roster_index(client, force=args.force)
                print(f"ESPN roster index warmed: {n_roster} names\n")
                if n_roster < 50:
                    print(
                        "  Warning: roster index is small — per-player legacy search will run "
                        "(slower). Check VPN/network if most players MISS.\n"
                    )

            for idx, name in enumerate(players, start=1):
                key = _normalize_key(name)
                if not args.include_facecard and has_ipl_facecard(conn, name):
                    skip += 1
                    continue

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

                resolved = None
                search_names = [name]
                repaired = _repair_pdf_name(name, canonical_index, conn=conn)
                if repaired and repaired.strip() and repaired.strip() not in search_names:
                    search_names.append(repaired.strip())
                for candidate in search_names:
                    resolved = resolve_espn_player(client, candidate, conn=conn)
                    if resolved:
                        break
                if not resolved:
                    miss += 1
                    print(f"  [{idx}] MISS  {name}")
                    if args.verbose:
                        print("       (legacy search + consumer API + roster index)")
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
