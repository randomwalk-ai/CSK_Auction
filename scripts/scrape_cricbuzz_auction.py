#!/usr/bin/env python3
"""
Scrape IPL auction results from Cricbuzz (all teams).

Cricbuzz loads auction rows with JavaScript — httpx/requests only get an empty shell.
This script uses Playwright (headless Chromium) to render the page and read the DOM.

URL (one page per year, all franchises):
  https://www.cricbuzz.com/cricket-series/ipl-{YEAR}/auction/completed

Setup (once):
  pip install playwright pandas httpx
  playwright install chromium

Usage:
  python scripts/scrape_cricbuzz_auction.py --years 2024
  python scripts/scrape_cricbuzz_auction.py --import-db
  python scripts/scrape_cricbuzz_auction.py --years 2008,2009,2024 --delay 2
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
OUT_CSV = ROOT / "data" / "cricbuzz_auction_all_teams.csv"
DB_PATH = ROOT / "auction_data.db"

DEFAULT_YEARS = list(range(2008, 2027))

TEAM_CODES = (
    "CSK", "MI", "RCB", "KKR", "DC", "DD", "RR", "SRH", "PBKS", "KXIP",
    "LSG", "GT", "RPS", "RPSG", "PWI", "KTK", "DEC",
)

ROLES = (
    "WK-Batter", "Wicket Keeper", "Wicket-Keeper", "All Rounder", "Allrounder",
    "Bowler", "Batter",
)

COUNTRIES = (
    "India", "Australia", "England", "South Africa", "New Zealand", "West Indies",
    "Pakistan", "Sri Lanka", "Afghanistan", "Bangladesh", "Zimbabwe", "Netherlands",
    "Ireland", "Scotland", "Nepal", "Oman", "USA", "United States of America",
)


def convert_to_cr(price_str: str) -> float:
    if not price_str or price_str in ("--", "-", ""):
        return 0.0
    s = str(price_str).strip()
    m = re.search(r"([\d.]+)", s)
    if not m:
        return 0.0
    v = float(m.group(1))
    if re.search(r"\bL\b", s, re.I) and "Cr" not in s:
        return round(v / 100, 4)
    return v


def _normalize_blob(text: str) -> str:
    """Insert spaces before glued keywords: 'SinghAllrounderIndia' -> readable tokens."""
    t = re.sub(r"\s+", " ", text).strip()
    keywords = [
        "Top Pick", "Base Price", "Final Price", "Team",
        *ROLES, *COUNTRIES,
        "unsold", "retained", "RETAINED", "traded", "sold",
        *TEAM_CODES,
    ]
    for kw in sorted(set(keywords), key=len, reverse=True):
        t = re.sub(rf"(?<=[a-z])(?={re.escape(kw)})", " ", t, flags=re.I)
        t = re.sub(rf"(?<=[A-Z])(?={re.escape(kw)})", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def parse_player_line(text: str, href: str, year: int) -> Optional[Dict]:
    raw_text = re.sub(r"\s+", " ", text).strip()
    if len(raw_text) < 8:
        return None

    status = "unknown"
    for st in ("retained", "unsold", "traded", "sold"):
        if re.search(rf"\b{st}\b", raw_text, re.I):
            status = st.lower()
            break

    text = _normalize_blob(raw_text)

    player_id = ""
    m_id = re.search(r"/auction/players/(\d+)", href)
    if m_id:
        player_id = m_id.group(1)

    team = ""
    for code in TEAM_CODES:
        if re.search(rf"\b{code}\b", text):
            team = code
            break

    role = ""
    for r in ROLES:
        if re.search(rf"\b{re.escape(r)}\b", text, re.I):
            role = r
            break

    country = ""
    for c in COUNTRIES:
        if re.search(rf"\b{re.escape(c)}\b", text, re.I):
            country = c
            break

    base_raw = ""
    final_raw = ""
    mb = re.search(r"Base Price\s*([\d.]+\s*(?:Cr|L)?)", text, re.I)
    mf = re.search(r"Final Price\s*([\d.]+\s*(?:Cr|L)?)", text, re.I)
    if mb:
        base_raw = mb.group(1).strip()
    if mf:
        final_raw = mf.group(1).strip()

    if not final_raw:
        prices = re.findall(r"([\d.]+\s*(?:Cr|L))", text, re.I)
        if status == "retained" and prices:
            final_raw = prices[-1]
        elif status == "sold" and len(prices) >= 2:
            base_raw = base_raw or prices[0]
            final_raw = prices[1]
        elif status == "sold" and len(prices) == 1:
            final_raw = prices[0]

    # Name: strip from start until role or country
    name = text
    cut = len(text)
    for token in [role, country, "Top Pick", "sold", "unsold", "retained", "Base Price"]:
        if token:
            idx = text.lower().find(token.lower())
            if idx > 2:
                cut = min(cut, idx)
    name = text[:cut].strip()
    name = re.sub(r"\bTop Pick\b", "", name, flags=re.I).strip()

    if not name or len(name) < 2:
        return None

    price_cr = convert_to_cr(final_raw) if final_raw and final_raw != "--" else 0.0

    return {
        "year": year,
        "player_name": name,
        "role": role,
        "country": country,
        "status": status,
        "base_price_raw": base_raw,
        "final_price_raw": final_raw,
        "price_cr": price_cr,
        "team_code": team,
        "team": team,
        "cricbuzz_player_id": player_id,
        "source": "cricbuzz_playwright",
    }


def _dismiss_overlays(page) -> None:
    """Close cookie/consent dialogs that block the auction list."""
    for selector in (
        "button:has-text('Accept')",
        "button:has-text('I Agree')",
        "button:has-text('Agree')",
        "[aria-label='Close']",
    ):
        try:
            loc = page.locator(selector).first
            if loc.count() > 0:
                loc.click(timeout=2000)
                page.wait_for_timeout(400)
        except Exception:
            pass


def _count_player_links(page) -> int:
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('a'))
            .filter(a => /\\/auction\\/players\\/\\d+/.test(a.getAttribute('href') || '')).length"""
    )


def _extract_dom_players(page) -> List[Dict]:
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('a'))
            .filter(a => /\\/auction\\/players\\/\\d+/.test(a.getAttribute('href') || ''))
            .map(a => ({
                href: a.href,
                text: (a.innerText || a.textContent || '').replace(/\\s+/g, ' ').trim()
            }))"""
    )


def _wait_for_auction_list(page, timeout_ms: int = 90000) -> bool:
    """
    Wait until player rows exist in DOM (attached — not necessarily 'visible').
    Playwright's default visible check often times out on Cricbuzz headless.
    """
    try:
        page.wait_for_function(
            """() => {
                const n = Array.from(document.querySelectorAll('a'))
                    .filter(a => /\\/auction\\/players\\/\\d+/.test(a.getAttribute('href') || '')).length;
                return n >= 3;
            }""",
            timeout=timeout_ms,
        )
        return True
    except Exception:
        return False


def scrape_year_playwright(
    page,
    year: int,
    scroll_passes: int = 30,
    debug_dir: Optional[Path] = None,
) -> List[Dict]:
    """Render page with Playwright and extract all player anchor rows."""
    urls = [
        f"https://www.cricbuzz.com/cricket-series/ipl-{year}/auction/completed",
        f"https://m.cricbuzz.com/cricket-series/ipl-{year}/auction/completed",
    ]

    raw_items: List[Dict] = []

    for url in urls:
        print(f"  {year}: loading {url}")
        try:
            page.goto(url, wait_until="load", timeout=90000)
            page.wait_for_timeout(3000)
            _dismiss_overlays(page)

            # Ensure we're on Completed tab (SPA)
            try:
                page.get_by_role("link", name="Completed").first.click(timeout=5000)
                page.wait_for_timeout(1500)
            except Exception:
                pass

            if not _wait_for_auction_list(page, timeout_ms=60000):
                # Scroll main content — list may render below fold
                for _ in range(8):
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(800)
                    if _count_player_links(page) >= 3:
                        break

            if not _wait_for_auction_list(page, timeout_ms=30000):
                count = _count_player_links(page)
                print(f"  {year}: only {count} player links on {url}")
                if debug_dir:
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(debug_dir / f"fail_{year}.png"), full_page=True)
                    html = page.content()
                    (debug_dir / f"fail_{year}.html").write_text(html[:500000], encoding="utf-8")
                continue

            # Lazy-load: scroll until count stabilizes
            last_count = 0
            stable = 0
            for _ in range(scroll_passes):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(700)
                count = _count_player_links(page)
                if count == last_count:
                    stable += 1
                    if stable >= 4:
                        break
                else:
                    stable = 0
                last_count = count

            raw_items = _extract_dom_players(page)
            if raw_items:
                print(f"  {year}: {len(raw_items)} DOM links from {url}")
                break

        except Exception as e:
            print(f"  {year}: error on {url} — {e}")
            if debug_dir:
                debug_dir.mkdir(parents=True, exist_ok=True)
                try:
                    page.screenshot(path=str(debug_dir / f"error_{year}.png"), full_page=True)
                except Exception:
                    pass

    if not raw_items:
        return []

    rows: List[Dict] = []
    seen = set()
    for item in raw_items:
        parsed = parse_player_line(item.get("text", ""), item.get("href", ""), year)
        if not parsed:
            continue
        key = (
            parsed["year"],
            parsed["player_name"].lower(),
            parsed["status"],
            parsed["team_code"],
            parsed["price_cr"],
        )
        if key in seen:
            continue
        seen.add(key)
        rows.append(parsed)

    print(f"  {year}: parsed {len(rows)} players")
    return rows


def save_csv(rows: List[Dict], path: Path) -> None:
    import pandas as pd

    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)
    print(f"Saved {len(rows)} rows → {path}")


def import_to_db(rows: List[Dict], db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auction_prices_full (
            player_name TEXT,
            year INTEGER,
            role TEXT,
            country TEXT,
            price REAL,
            status TEXT,
            team TEXT,
            team_code TEXT,
            base_price_cr REAL,
            cricbuzz_player_id TEXT,
            source TEXT
        )
        """
    )
    conn.execute("DELETE FROM auction_prices_full WHERE source = 'cricbuzz_playwright'")

    for row in rows:
        conn.execute(
            """
            INSERT INTO auction_prices_full
            (player_name, year, role, country, price, status, team, team_code,
             base_price_cr, cricbuzz_player_id, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["player_name"],
                row["year"],
                row["role"],
                row["country"],
                row["price_cr"],
                row["status"],
                row["team"],
                row["team_code"],
                convert_to_cr(row.get("base_price_raw", "")),
                row.get("cricbuzz_player_id", ""),
                "cricbuzz_playwright",
            ),
        )
        if row["status"] == "sold" and row["price_cr"] > 0:
            conn.execute(
                """
                INSERT OR REPLACE INTO auction_prices
                (player_name, year, role, country, price, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    row["player_name"],
                    row["year"],
                    row["role"],
                    row["country"],
                    row["price_cr"],
                    f"{row['team_code']} | playwright",
                ),
            )

    conn.commit()
    conn.close()
    print(f"Imported into {db_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Scrape Cricbuzz IPL auction via Playwright (JS-rendered)"
    )
    parser.add_argument("--years", type=str, default="", help="e.g. 2008,2024")
    parser.add_argument("--delay", type=float, default=2.0, help="Seconds between years")
    parser.add_argument("--import-db", action="store_true")
    parser.add_argument("--out", type=str, default=str(OUT_CSV))
    parser.add_argument("--headed", action="store_true", help="Show browser window")
    parser.add_argument(
        "--browser",
        choices=("chromium", "chrome"),
        default="chrome",
        help="Use installed Google Chrome (recommended — headless Chromium is often blocked)",
    )
    parser.add_argument("--debug", action="store_true", help="Save screenshot/HTML on failure")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Install Playwright first:")
        print("  pip install playwright")
        print("  playwright install chromium")
        return

    years = DEFAULT_YEARS if not args.years else [int(y.strip()) for y in args.years.split(",")]

    print("Cricbuzz scraper (Playwright — handles JavaScript-rendered auction list)\n")

    debug_dir = ROOT / "data" / "scrape_debug" if args.debug else None

    all_rows: List[Dict] = []
    with sync_playwright() as p:
        launch_kw = {
            "headless": not args.headed,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if args.browser == "chrome":
            try:
                browser = p.chromium.launch(channel="chrome", **launch_kw)
                print("Using Google Chrome (channel=chrome)\n")
            except Exception as e:
                print(f"Chrome not found ({e}), falling back to Chromium")
                print("Install Chrome or run: playwright install chromium\n")
                browser = p.chromium.launch(**launch_kw)
        else:
            browser = p.chromium.launch(**launch_kw)

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1400, "height": 1000},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
        )
        page = context.new_page()

        for year in years:
            all_rows.extend(scrape_year_playwright(page, year, debug_dir=debug_dir))
            time.sleep(args.delay)

        browser.close()

    if not all_rows:
        print("\nNo players scraped.")
        print("Try:")
        print("  python scripts/scrape_cricbuzz_auction.py --years 2024 --headed --browser chrome --debug")
        print("Check data/scrape_debug/ for screenshots if --debug was used.")
        return

    save_csv(all_rows, Path(args.out))
    if args.import_db:
        import_to_db(all_rows, DB_PATH)

    sold = sum(1 for r in all_rows if r["status"] == "sold" and r["price_cr"] > 0)
    print(f"\nDone. Total: {len(all_rows)} | sold with price: {sold}")


if __name__ == "__main__":
    main()
