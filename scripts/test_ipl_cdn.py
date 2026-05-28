#!/usr/bin/env python3
"""Test IPL portrait sources (run on your Mac)."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))

from ipl_facecards import (  # noqa: E402
    IPL_UA,
    build_squad_index,
    debug_nearest_headshot_for_slug,
    facecard_url_candidates,
    find_working_facecard_url,
    lookup_in_squad_index,
)


def main() -> int:
    print("Note: /players/ms-dhoni/1 uses route id 1, NOT the IPLHeadshot CDN id.\n")

    with httpx.Client(follow_redirects=True, trust_env=False, timeout=25) as client:
        print("Building squad index from 10 team pages…")
        index = build_squad_index(client)
        print(f"Squad index: {len(index)} players\n")

        for name in ("MS Dhoni", "Sam Curran", "Virat Kohli", "Ruturaj Gaikwad"):
            match = lookup_in_squad_index(name, index)
            if not match:
                print(f"  {name}: not in squad index")
                continue
            print(
                f"  {name}: ipl_id={match['id']}  matched={match['display_name']}  "
                f"team={match['team']}"
            )
            if match.get("facecard_url"):
                print(f"    squad URL: {match['facecard_url']}")

            url, year = find_working_facecard_url(
                client, match["id"], prefer_url=match.get("facecard_url"),
            )
            print(f"    verified: {'OK' if url else 'MISS'}  {url or ''}  year={year}")

        print("\n=== Route id 1 (profile URL) vs squad headshot id ===")
        print("  HEAD often 404 on S3 — using GET:")
        for u in facecard_url_candidates("1")[:4]:
            r = client.get(u, headers=IPL_UA)
            ok = r.status_code == 200 and len(r.content) > 400
            print(f"    {r.status_code} {len(r.content):>6}  {'OK' if ok else 'fail'}  {u}")

        dhoni = lookup_in_squad_index("MS Dhoni", index)
        if dhoni:
            print(f"\n  Dhoni squad headshot id={dhoni['id']} (use this, not route id 1)")
        else:
            print("\n  MS Dhoni still missing — nearest-headshot debug (CSK squad):")
            r = client.get(
                "https://www.iplt20.com/teams/chennai-super-kings/squad",
                headers=IPL_UA,
            )
            text = r.text or ""
            for slug in ("ms-dhoni", "sam-curran", "ruturaj-gaikwad"):
                dbg = debug_nearest_headshot_for_slug(text, slug)
                if dbg:
                    print(
                        f"    {slug}: nearest id={dbg['ipl_player_id']}  "
                        f"dist={dbg['distance_chars']}  {dbg['facecard_url']}"
                    )

    print("\nNext: python3 scripts/seed_ipl_facecards.py --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
