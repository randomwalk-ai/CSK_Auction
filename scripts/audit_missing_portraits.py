#!/usr/bin/env python3
"""
Audit auction-pool players missing IPL face cards — IPL + ESPN only (no Cricbuzz).

Usage (from auction-data-pipeline/):
  python3 scripts/audit_missing_portraits.py
  python3 scripts/audit_missing_portraits.py --probe-ipl
  python3 scripts/audit_missing_portraits.py --limit 30
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
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
    find_working_facecard_url,
    has_ipl_facecard,
    lookup_in_squad_index,
)


def _portrait_row(conn: sqlite3.Connection, player_key: str):
    return conn.execute(
        """
        SELECT source, image_url, LENGTH(image_data)
        FROM player_portraits WHERE player_key = ?
        """,
        (player_key,),
    ).fetchone()


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit missing IPL face cards (IPL/ESPN only)")
    parser.add_argument("--probe-ipl", action="store_true", help="GET-verify IPL headshot URLs")
    parser.add_argument("--limit", type=int, default=0, help="Max missing players to detail")
    args = parser.parse_args()

    conn = _connect()
    all_names = collect_portrait_names()
    missing = [n for n in all_names if not has_ipl_facecard(conn, n)]
    espn_n = conn.execute("SELECT COUNT(*) FROM player_espn_cricinfo").fetchone()[0]
    print(f"Database: {DB_PATH}")
    print(f"Pool players: {len(all_names)}  |  IPL face cards: {len(all_names) - len(missing)}")
    print(f"Missing IPL face card: {len(missing)}  |  ESPN mappings: {espn_n}\n")

    if not missing:
        print("Everyone has an IPL face card row.")
        conn.close()
        return 0

    index = {}
    if args.probe_ipl:
        print("Building IPL squad index (10 teams)…")
        with httpx.Client(follow_redirects=True, trust_env=False, timeout=30.0) as client:
            index = build_squad_index(client)
        print(f"Squad index: {len(index)} players\n")

    show = missing if args.limit <= 0 else missing[: args.limit]
    buckets: dict[str, list[str]] = {
        "ipl_squad_ok": [],
        "ipl_squad_no_cdn": [],
        "ipl_not_on_squad": [],
        "espn_only": [],
        "initials_only": [],
    }

    print(f"{'Player':<28} {'Squad':^6} {'CDN':^5} {'Cache':^14}")
    print("-" * 58)

    with httpx.Client(follow_redirects=True, trust_env=False, timeout=20.0) as client:
        for name in show:
            pk = _normalize_key(name)
            squad = "—"
            cdn = "—"
            if index:
                match = lookup_in_squad_index(name, index)
                if match:
                    squad = "yes"
                    if args.probe_ipl:
                        url, _y = find_working_facecard_url(
                            client,
                            match["id"],
                            prefer_url=match.get("facecard_url"),
                        )
                        if url:
                            cdn = "OK"
                            buckets["ipl_squad_ok"].append(name)
                        else:
                            cdn = "fail"
                            buckets["ipl_squad_no_cdn"].append(name)
                    else:
                        buckets["ipl_squad_ok"].append(name)
                else:
                    squad = "no"
                    buckets["ipl_not_on_squad"].append(name)

            prow = _portrait_row(conn, pk)
            cache = "—"
            if prow:
                src, _url, nbytes = prow[0], prow[1] or "", prow[2] or 0
                cache = f"{src}({nbytes}b)"
                if src == "espncricinfo" and nbytes and nbytes > 800:
                    buckets["espn_only"].append(name)
                elif src == "initials":
                    buckets["initials_only"].append(name)

            print(f"{name:<28} {squad:^6} {cdn:^5} {cache:^14}")

    conn.close()

    if args.limit and len(missing) > args.limit:
        print(f"\n… and {len(missing) - args.limit} more missing (re-run without --limit)")

    print("\n=== Summary ===")
    if index:
        print(f"  IPL squad + CDN OK:     {len(buckets['ipl_squad_ok'])}  → seed_ipl_facecards.py --apply")
        print(f"  IPL squad, CDN fail:    {len(buckets['ipl_squad_no_cdn'])}")
        print(f"  Not on IPL squad HTML:  {len(buckets['ipl_not_on_squad'])}  → seed_espn_portraits.py")
    print(f"  Cached ESPN photo:        {len(set(buckets['espn_only']))}")
    print(f"  Cached initials only:   {len(set(buckets['initials_only']))}")
    print("\n  Portrait chain: IPL facecard → ESPN → initials (Cricbuzz not used)")

    notable = [n for n in ("MS Dhoni", "Sam Curran", "Virat Kohli") if n in missing]
    if notable:
        print(f"  Notable missing face card: {', '.join(notable)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
