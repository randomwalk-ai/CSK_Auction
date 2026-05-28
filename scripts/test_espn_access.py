#!/usr/bin/env python3
"""Quick ESPN reachability check (legacy search + roster + resolve)."""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

if os.getenv("ESPN_DEBUG", "").strip().lower() in ("1", "true", "yes"):
    logging.basicConfig(level=logging.DEBUG)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))

from env_loader import load_project_dotenv

load_project_dotenv()

from espn_cricinfo import (  # noqa: E402
    LEGACY_PLAYER_SEARCH,
    _espn_cookie_string,
    _espn_document_headers,
    _html_has_next_data,
    _lookup_roster_index,
    _pick_image_url,
    _playwright_auto,
    _player_link_from_search_html,
    _scrape_profile_page,
    _scrape_profile_playwright,
    _use_playwright,
    espn_connectivity_self_check,
    espn_http_client,
    resolve_espn_player,
    warm_espn_roster_index,
)


def main() -> int:
    print(f"ESPN cookies loaded: {bool(_espn_cookie_string())}")
    print(f"ESPN_USE_PLAYWRIGHT: {_use_playwright()}")
    print(f"ESPN_PLAYWRIGHT_AUTO: {_playwright_auto()}\n")

    with espn_http_client() as client:
        for msg in espn_connectivity_self_check(client):
            print(f"  {msg}")

        html = ""
        try:
            resp = client.get(
                LEGACY_PLAYER_SEARCH,
                params={"search": "Sam Curran"},
                headers=_espn_document_headers(),
                timeout=20.0,
            )
            html = resp.text or ""
            print(f"\nLegacy search: HTTP {resp.status_code}  len={len(html)}")
            print(f"  /cricketers/ in HTML: {'yes' if '/cricketers/' in html else 'no'}")
        except Exception as exc:
            print(f"\nLegacy search failed: {exc}")

        link = _player_link_from_search_html(html, "Sam Curran") if html else None
        if link:
            slug, pid = link
            print(f"  Sam Curran link: {slug}-{pid}")
            probe = _scrape_profile_page(client, slug, pid)
            print(f"\nProfile probe ({slug}-{pid}): {'OK' if probe else 'MISS'}")
            if probe:
                img = _pick_image_url(probe) or probe.get("imageUrl", "")
                print(f"  image: {str(img)[:90]}")
            else:
                resp2 = client.get(
                    f"https://www.espncricinfo.com/cricketers/{slug}-{pid}",
                    headers=_espn_document_headers(),
                    timeout=20.0,
                )
                body = resp2.text or ""
                print(
                    f"  profile HTTP {resp2.status_code}  "
                    f"__NEXT_DATA__={_html_has_next_data(body)}  "
                    f"hscicdn={bool(re.search(r'hscicdn', body, re.I))}"
                )
        else:
            print("\nProfile probe: skipped (name match not found in search HTML)")

        print("\nWarming roster index… (force=True reparses squad JSON for portrait URLs)")
        try:
            n = warm_espn_roster_index(client, force=True)
            print(f"Roster warm: {n} names")
        except Exception as exc:
            print(f"Roster warm failed: {exc}")
            n = 0

        hit = _lookup_roster_index("Sam Curran")
        if hit:
            print(
                f"  Roster lookup Sam Curran: id={hit['espn_player_id']} "
                f"slug={hit['espn_slug']}  has_image={bool(hit.get('raw_image_url'))}"
            )
            if hit.get("raw_image_url"):
                print(f"    roster image: {hit['raw_image_url'][:90]}")
        else:
            print("  Roster lookup Sam Curran: not in IPL 2026 squads index")

        if hit and not hit.get("raw_image_url"):
            print("\nDirect Playwright probe (Sam Curran)…")
            pw = _scrape_profile_playwright(hit["espn_slug"], hit["espn_player_id"])
            print(f"  Playwright: {'OK' if pw else 'MISS'}")
            if pw:
                print(f"  {(_pick_image_url(pw) or pw.get('imageUrl', ''))[:95]}")

        try:
            resolved = resolve_espn_player(client, "Sam Curran")
            print(
                f"\nresolve_espn_player('Sam Curran'): "
                f"{'OK' if resolved else 'MISS'}"
            )
            if resolved:
                print(f"  {resolved['uniform_image_url'][:95]}")
        except Exception as exc:
            print(f"\nresolve_espn_player failed: {exc}")

        if n >= 50:
            for trial in ("Akash Deep", "Josh Hazlewood"):
                r = resolve_espn_player(client, trial)
                print(f"  {trial}: {'OK' if r else 'MISS'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
