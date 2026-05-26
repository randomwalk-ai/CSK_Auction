#!/usr/bin/env python3
"""
Scrape Cricbuzz IPL "Players Targeted" → "See All Bids" for every franchise, every year.

Flow per season:
  1. Open https://www.cricbuzz.com/cricket-series/ipl-{YEAR}/auction/teams
  2. Collect team links (/auction/teams/{id})
  3. For each team page (e.g. Chennai Super Kings), find the **Players Targeted**
     section and click the green **See All Bids** button
  4. Parse the **All Bids** modal: Player | Bids | Last Bid | Bid War (Won/Lost)

Output:
  data/cricbuzz_all_bids/{YEAR}/{TEAM}.csv   (team-wise)
  data/cricbuzz_all_bids/all_bids_master.csv (combined)

Exact flow (automated):
  ipl-{YEAR}/auction/teams → TEAMS tab → each franchise one-by-one
  → Players Targeted → See All Bids → scrape modal → save → next team → next year
DB (optional --import-db):
  bid_history table (viewing_team_code = franchise whose bid sheet this is)

Setup:
  pip install playwright pandas
  playwright install chromium   # or use --browser chrome

Usage:
  python scripts/scrape_cricbuzz_all_bids.py --browser chrome
  python scripts/scrape_cricbuzz_all_bids.py --import-db --browser chrome --delay 2
  python scripts/scrape_cricbuzz_all_bids.py --years 2026 --teams CSK
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "cricbuzz_all_bids"
OUT_CSV = OUT_DIR / "all_bids_master.csv"
CACHE_DIR = OUT_DIR / "_cache"
DB_PATH = ROOT / "auction_data.db"

# Boots the auction SPA; other years selected via dropdown on this page.
AUCTION_HUB_URL = "https://www.cricbuzz.com/cricket-series/ipl-2026/auction/teams"

DEFAULT_YEARS = list(range(2008, 2027))

EXTRACT_BIDS_JS = """
() => {
  const out = [];
  for (const d of document.querySelectorAll('div.grid')) {
    const kids = Array.from(d.children).map(c => (c.innerText || '').trim());
    if (kids.length !== 4) continue;
    if (!/^(Won|Lost)$/i.test(kids[3])) continue;
    const lines = kids[0].split('\\n').map(s => s.trim()).filter(Boolean);
    out.push({
      player_name: lines[0] || '',
      role: lines[1] || '',
      country: lines[2] || '',
      num_bids: parseInt(kids[1], 10) || 0,
      last_bid_raw: kids[2],
      bid_war: kids[3],
    });
  }
  return out;
}
"""

SCROLL_MODAL_JS = """
() => {
  const h = Array.from(document.querySelectorAll('h3,h2')).find(
    el => /All Bids/i.test((el.innerText || '').trim())
  );
  if (!h) return false;
  let scrollEl = h.parentElement;
  for (let i = 0; i < 12 && scrollEl; i++) {
    const style = window.getComputedStyle(scrollEl);
    if (
      (style.overflowY === 'auto' || style.overflowY === 'scroll') &&
      scrollEl.scrollHeight > scrollEl.clientHeight + 20
    ) break;
    scrollEl = scrollEl.parentElement;
  }
  if (!scrollEl) return false;
  let last = -1;
  for (let i = 0; i < 25; i++) {
    scrollEl.scrollTop = scrollEl.scrollHeight;
    const n = scrollEl.scrollHeight;
    if (n === last) break;
    last = n;
  }
  return true;
}
"""

# Longest-first so PBKS matches before PB, etc.
IPL_TEAM_CODES = (
    "RPSG", "PBKS", "KXIP", "LSG", "CSK", "KKR", "RCB", "SRH", "MI",
    "DCG", "DEC", "PWI", "KTK", "RPS", "DD", "DC", "RR", "GT",
)

TEAM_LINKS_JS = """
() => {
  const KNOWN = %s;
  const seen = new Set();
  const teams = [];
  const origin = window.location.origin;

  for (const a of document.querySelectorAll('a[href*="/auction/teams/"]')) {
    let hrefRaw = (a.getAttribute('href') || a.href || '').split('#')[0].trim();
    if (!hrefRaw) continue;
    let href = hrefRaw.startsWith('http') ? hrefRaw : origin + hrefRaw;
    const m = href.match(/\\/auction\\/teams\\/(\\d+)/);
    if (!m) continue;
    href = href.split('#')[0];
    if (seen.has(href)) continue;
    seen.add(href);

    const raw = (a.innerText || a.textContent || '').trim().replace(/\\s+/g, ' ');
    let code = '';
    for (const c of KNOWN) {
      if (raw.startsWith(c)) { code = c; break; }
    }
    if (!code) {
      const mm = raw.match(/^([A-Z]{2,5})\\b/);
      code = mm ? mm[1] : '';
    }
    if (!code) continue;

    teams.push({ team_id: m[1], team_code: code, url: href });
  }
  return teams;
}
""" % repr(list(IPL_TEAM_CODES))


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


# Latest season URL boots the auction SPA reliably; older years via dropdown.


def _teams_index_url(year: int) -> str:
    return f"https://www.cricbuzz.com/cricket-series/ipl-{year}/auction/teams"


def _dismiss_overlays(page) -> None:
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


def _wait_for_team_links(page, year: int, timeout_ms: int = 75000) -> bool:
    """Cricbuzz fills team cards via JS — plain load+2s is too fast (especially headless)."""
    try:
        page.wait_for_function(
            """() => {
                const links = Array.from(document.querySelectorAll('a'))
                  .filter(a => {
                    const h = (a.getAttribute('href') || a.href || '');
                    return /\\/auction\\/teams\\/\\d+/.test(h);
                  });
                return links.length >= 3;
            }""",
            timeout=timeout_ms,
        )
        return True
    except Exception:
        return False


def _select_auction_year(page, year: int) -> bool:
    """Change IPL year via the on-page <select> and wait for header to update."""
    year_s = str(year)
    try:
        changed = page.evaluate(
            """(y) => {
              for (const s of document.querySelectorAll('select')) {
                const opt = Array.from(s.options).find(
                  o => o.value === y || (o.textContent || '').trim() === y
                );
                if (opt) {
                  s.value = opt.value;
                  s.dispatchEvent(new Event('change', { bubbles: true }));
                  return true;
                }
              }
              return false;
            }""",
            year_s,
        )
        if not changed:
            return False
        page.wait_for_timeout(2500)
        try:
            page.wait_for_function(
                """(y) => {
                  const h = (document.querySelector('h1')?.innerText || '');
                  return h.includes('IPL Auction') && h.includes(String(y));
                }""",
                arg=year,
                timeout=20000,
            )
        except Exception:
            pass
        return _auction_spa_ready(page, year)
    except Exception:
        return False


def _auction_spa_ready(page, year: int) -> bool:
    """Generic SSR shell shows h1 'Cricket Teams'; real page shows 'IPL Auction {year}'."""
    try:
        h1 = page.evaluate(
            "() => (document.querySelector('h1')?.innerText || '').trim()"
        )
        return "IPL Auction" in (h1 or "") and str(year) in (h1 or "")
    except Exception:
        return False


def _page_auction_diag(page) -> Dict:
    try:
        return page.evaluate(
            """() => {
              const links = Array.from(document.querySelectorAll('a'))
                .filter(a => /\\/auction\\/teams\\/\\d+/.test(a.getAttribute('href') || a.href || ''));
              return {
                title: document.querySelector('h1')?.innerText || '',
                team_link_count: links.length,
                sample_hrefs: links.slice(0, 3).map(a => a.getAttribute('href') || a.href),
              };
            }"""
        )
    except Exception:
        return {}


def _discover_years_on_page(page) -> List[int]:
    """Read all IPL years from the auction page dropdown."""
    try:
        years = page.evaluate(
            """() => {
              for (const s of document.querySelectorAll('select')) {
                const ys = Array.from(s.options)
                  .map(o => parseInt((o.value || o.textContent || '').trim(), 10))
                  .filter(y => y >= 2008 && y <= 2035);
                if (ys.length >= 5) return [...new Set(ys)].sort((a, b) => a - b);
              }
              return [];
            }"""
        )
        return [int(y) for y in years] if years else []
    except Exception:
        return []


def _team_cache_path(year: int) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"teams_{year}.json"


def _load_team_cache(year: int) -> List[Dict]:
    path = _team_cache_path(year)
    if not path.exists():
        return []
    try:
        teams = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(teams, list) and teams:
            print(f"  [{year}] using cached team list ({len(teams)} teams) → {path}")
            return teams
    except Exception:
        pass
    return []


def _save_team_cache(year: int, teams: List[Dict]) -> None:
    _team_cache_path(year).write_text(json.dumps(teams, indent=2), encoding="utf-8")


def _bootstrap_auction_app(page) -> bool:
    """Load the auction SPA once (hub URL). Required before year/team navigation."""
    try:
        page.goto(AUCTION_HUB_URL, wait_until="load", timeout=120000)
        page.wait_for_timeout(3000)
        _dismiss_overlays(page)
        try:
            page.wait_for_function(
                """() => (document.querySelector('h1')?.innerText || '').includes('IPL Auction')""",
                timeout=30000,
            )
            return True
        except Exception:
            diag = _page_auction_diag(page)
            return diag.get("team_link_count", 0) >= 3
    except Exception as e:
        print(f"  bootstrap failed — {e}")
        return False


def _open_teams_index(page, year: int, *, already_on_hub: bool = False) -> bool:
    """
    Open Teams index for YEAR: select year dropdown → TEAMS tab → wait for franchise cards.
    """
    print(f"  [{year}] Teams index (year dropdown → TEAMS tab)")

    if not already_on_hub:
        if not _bootstrap_auction_app(page):
            diag = _page_auction_diag(page)
            print(
                f"  [{year}] auction SPA not loaded | h1={diag.get('title')} | "
                f"links={diag.get('team_link_count')}"
            )

    if _select_auction_year(page, year):
        _click_auction_teams_tab(page)
        if _wait_for_team_links(page, year, timeout_ms=20000):
            return True

    direct = _teams_index_url(year)
    print(f"  [{year}] retry direct URL → {direct}")
    try:
        page.goto(direct, wait_until="load", timeout=90000)
        page.wait_for_timeout(2500)
        _dismiss_overlays(page)
        # Already on /auction/teams — do NOT click header Teams
        if not _on_auction_teams_index(page):
            _click_auction_teams_tab(page)
        if _auction_spa_ready(page, year) and _wait_for_team_links(page, year, timeout_ms=20000):
            return True
    except Exception as e:
        print(f"  [{year}] direct URL failed — {e}")

    diag = _page_auction_diag(page)
    print(
        f"  [{year}] Teams index failed | h1={diag.get('title')} | links={diag.get('team_link_count')}"
    )
    return False


def _return_to_teams_index(page, year: int) -> bool:
    """After scraping one team, go back to franchise list for the next."""
    try:
        page.go_back()
        page.wait_for_timeout(1500)
        _click_auction_teams_tab(page)
        if _wait_for_team_links(page, year, timeout_ms=12000):
            return True
    except Exception:
        pass
    return _open_teams_index(page, year, already_on_hub=False)


def _on_auction_teams_index(page) -> bool:
    """True when URL or DOM shows the franchise list (not global Teams page)."""
    try:
        return bool(
            page.evaluate(
                """() => {
                  const path = location.pathname || '';
                  if (path.includes('/auction/teams') && !path.match(/\\/teams\\/\\d+/)) return true;
                  const links = Array.from(document.querySelectorAll('a'))
                    .filter(a => /\\/auction\\/teams\\/\\d+/.test(a.getAttribute('href') || a.href || ''));
                  return links.length >= 3;
                }"""
            )
        )
    except Exception:
        return False


def _click_auction_teams_tab(page) -> None:
    """
    Click TEAMS in the auction sub-menu (Live | Completed | Players | Teams),
    NOT the main site header 'Teams' link at the top.
    """
    if _on_auction_teams_index(page):
        return

    clicked = page.evaluate(
        """() => {
          for (const a of document.querySelectorAll('a')) {
            if ((a.innerText || '').trim() !== 'Teams') continue;
            let el = a.parentElement;
            for (let i = 0; i < 10 && el; i++) {
              const txt = el.innerText || '';
              if (txt.includes('Live') && txt.includes('Completed') && txt.includes('Players')) {
                a.click();
                return true;
              }
              el = el.parentElement;
            }
          }
          return false;
        }"""
    )
    if clicked:
        page.wait_for_timeout(1500)
        return

    # Fallback: href contains /auction/teams but not /teams/{id}
    try:
        tab = page.locator('a[href*="/auction/teams"]:not([href*="/auction/teams/"])').first
        if tab.count() == 0:
            tab = page.locator('a[href$="/auction/teams"]').first
        if tab.count() > 0:
            tab.click(timeout=8000)
            page.wait_for_timeout(1500)
    except Exception:
        pass


def _click_teams_tab(page) -> None:
    """Alias — always use auction sub-nav, never header Teams."""
    _click_auction_teams_tab(page)


def _collect_teams_on_index(page, year: int) -> List[Dict]:
    if not _wait_for_team_links(page, year, timeout_ms=30000):
        for _ in range(8):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(700)
            if _wait_for_team_links(page, year, timeout_ms=15000):
                break

    teams = page.evaluate(TEAM_LINKS_JS) or []
    by_id: Dict[str, Dict] = {}
    for t in teams:
        by_id[t["team_id"]] = t
    teams = list(by_id.values())
    if teams:
        codes = ", ".join(t["team_code"] for t in teams)
        print(f"  [{year}] TEAMS tab: {len(teams)} franchises → {codes}")
    return teams


def discover_teams(page, year: int) -> List[Dict]:
    if _open_teams_index(page, year):
        teams = _collect_teams_on_index(page, year)
        if teams:
            _save_team_cache(year, teams)
            return teams
    cached = _load_team_cache(year)
    if cached:
        return cached
    return []


def _click_team_on_index(page, team: Dict) -> bool:
    team_id = team["team_id"]
    try:
        card = page.locator(f'a[href*="/auction/teams/{team_id}"]').first
        card.scroll_into_view_if_needed(timeout=8000)
        card.click(timeout=12000)
        page.wait_for_timeout(2000)
        return True
    except Exception:
        return False


def _click_players_targeted_see_all_bids(page) -> bool:
    """
    On a team page, open the modal from:
      Players Targeted  [See All Bids]
    """
    try:
        page.wait_for_function(
            """() => (document.body.innerText || '').includes('Players Targeted')""",
            timeout=20000,
        )
    except Exception:
        pass

    # Prefer the button inside the Players Targeted header row
    clicked = page.evaluate(
        """() => {
          const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
          for (const el of document.querySelectorAll('div, section')) {
            const t = norm(el.innerText);
            if (!t.includes('Players Targeted')) continue;
            const btn = Array.from(el.querySelectorAll('button')).find(
              b => /see all bids/i.test(norm(b.innerText))
            );
            if (btn) { btn.click(); return true; }
          }
          const any = Array.from(document.querySelectorAll('button')).find(
            b => /see all bids/i.test(norm(b.innerText))
          );
          if (any) { any.click(); return true; }
          return false;
        }"""
    )
    if clicked:
        page.wait_for_timeout(1500)
        return True

    try:
        loc = page.get_by_role("button", name=re.compile(r"see all bids", re.I))
        if loc.count() > 0:
            loc.first.scroll_into_view_if_needed(timeout=5000)
            loc.first.click(timeout=8000)
            page.wait_for_timeout(1500)
            return True
    except Exception:
        pass
    return False


def _team_display_name(page) -> str:
    try:
        return page.evaluate(
            """() => {
              const t = document.body.innerText || '';
              const m = t.match(/(Chennai Super Kings|Mumbai Indians|Royal Challengers[^\\n]+|Kolkata Knight Riders|Delhi Capitals|Sunrisers Hyderabad|Rajasthan Royals|Punjab Kings|Gujarat Titans|Lucknow Super Giants|Rising Pune Supergiant|Kochi Tuskers Kerala|Deccan Chargers)/);
              return m ? m[1] : '';
            }"""
        ) or ""
    except Exception:
        return ""


def _scrape_all_bids_modal(
    page,
    year: int,
    team: Dict,
    debug_dir: Optional[Path] = None,
) -> List[Dict]:
    """Scrape All Bids modal on current team page (already opened)."""
    team_code = team["team_code"]
    team_id = team["team_id"]
    rows: List[Dict] = []

    if not _click_players_targeted_see_all_bids(page):
        print(f"    [{year}] {team_code}: no Players Targeted / See All Bids")
        return []

    try:
        page.wait_for_function(
            """() => Array.from(document.querySelectorAll('h3,h2'))
                .some(h => /All Bids/i.test((h.innerText||'')))""",
            timeout=15000,
        )
    except Exception:
        print(f"    [{year}] {team_code}: All Bids modal did not open")
        if debug_dir:
            page.screenshot(path=str(debug_dir / f"modal_fail_{year}_{team_code}.png"))
        return []

    for _ in range(6):
        page.evaluate(SCROLL_MODAL_JS)
        page.wait_for_timeout(400)

    raw = page.evaluate(EXTRACT_BIDS_JS) or []
    team_name = _team_display_name(page)
    seen = set()
    for r in raw:
        name = (r.get("player_name") or "").strip()
        if not name or len(name) < 2:
            continue
        key = (name.lower(), r.get("last_bid_raw"), r.get("bid_war"))
        if key in seen:
            continue
        seen.add(key)
        last_raw = r.get("last_bid_raw") or ""
        rows.append({
            "year": year,
            "viewing_team_code": team_code,
            "viewing_team_id": team_id,
            "viewing_team_name": team_name,
            "player_name": name,
            "role": r.get("role") or "",
            "country": r.get("country") or "",
            "num_bids": int(r.get("num_bids") or 0),
            "last_bid_raw": last_raw,
            "last_bid_cr": convert_to_cr(last_raw),
            "bid_war": (r.get("bid_war") or "").strip().lower(),
            "source": "cricbuzz_all_bids_playwright",
        })

    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    except Exception:
        pass

    return rows


def scrape_team_all_bids(
    page,
    year: int,
    team: Dict,
    debug_dir: Optional[Path] = None,
) -> List[Dict]:
    team_code = team["team_code"]
    try:
        print(f"    [{year}] {team_code}: click team → Players Targeted → See All Bids")
        _dismiss_overlays(page)
        rows = _scrape_all_bids_modal(page, year, team, debug_dir=debug_dir)
        print(f"    [{year}] {team_code}: {len(rows)} bid rows")
        return rows

    except Exception as e:
        print(f"    [{year}] {team_code}: error — {e}")
        if debug_dir:
            try:
                page.screenshot(path=str(debug_dir / f"error_{year}_{team_code}.png"))
            except Exception:
                pass

    return []


def scrape_year(
    page,
    year: int,
    team_filter: Optional[set],
    out_dir: Path,
    debug_dir: Optional[Path],
) -> List[Dict]:
    print(f"\n{'='*60}\nYEAR {year}: TEAMS tab → each team → See All Bids → save CSV\n{'='*60}")

    teams = discover_teams(page, year)
    if not teams:
        print(f"  [{year}] skip — no teams (run once with --headed to build cache)")
        return []

    if team_filter:
        teams = [t for t in teams if t["team_code"] in team_filter]

    on_index = _open_teams_index(page, year)
    if not on_index:
        print(f"  [{year}] Teams index unavailable — opening each team URL directly")

    all_rows: List[Dict] = []
    for i, team in enumerate(teams, start=1):
        code = team["team_code"]
        print(f"  [{year}] team {i}/{len(teams)}: {code}")

        if on_index:
            if not _click_team_on_index(page, team):
                print(f"    [{year}] {code}: click failed — using URL")
                page.goto(team["url"], wait_until="load", timeout=90000)
                page.wait_for_timeout(2000)
        else:
            page.goto(team["url"], wait_until="load", timeout=90000)
            page.wait_for_timeout(2000)

        rows = scrape_team_all_bids(page, year, team, debug_dir=debug_dir)
        if rows:
            save_team_csv(rows, out_dir / str(year) / f"{code}.csv")
            all_rows.extend(rows)

        if on_index and i < len(teams):
            if not _return_to_teams_index(page, year):
                print(f"  [{year}] re-opening Teams index after {code}")
                on_index = _open_teams_index(page, year)
        time.sleep(0.5)

    print(f"  [{year}] done — {len(all_rows)} bid rows | files in {out_dir / str(year)}")
    return all_rows


def save_team_csv(rows: List[Dict], path: Path) -> None:
    import pandas as pd

    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)
    print(f"      saved → {path}")


def save_csv(rows: List[Dict], path: Path) -> None:
    import pandas as pd

    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)
    print(f"Saved {len(rows)} rows → {path}")


def import_to_db(rows: List[Dict], db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bid_history (
            year INTEGER,
            player_name TEXT,
            role TEXT,
            country TEXT,
            bids INTEGER,
            last_bid TEXT,
            result TEXT
        )
        """
    )
    existing = {r[1] for r in conn.execute("PRAGMA table_info(bid_history)")}
    migrations = {
        "viewing_team_code": "TEXT",
        "viewing_team_id": "TEXT",
        "viewing_team_name": "TEXT",
        "num_bids": "INTEGER",
        "last_bid_raw": "TEXT",
        "last_bid_cr": "REAL",
        "bid_war": "TEXT",
        "source": "TEXT",
    }
    for col, typ in migrations.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE bid_history ADD COLUMN {col} {typ}")

    conn.execute(
        "DELETE FROM bid_history WHERE source = 'cricbuzz_all_bids_playwright'"
    )

    for row in rows:
        conn.execute(
            """
            INSERT INTO bid_history
            (year, viewing_team_code, viewing_team_id, viewing_team_name,
             player_name, role, country, num_bids, last_bid_raw, last_bid_cr,
             bid_war, source, bids, last_bid, result)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["year"],
                row["viewing_team_code"],
                row["viewing_team_id"],
                row["viewing_team_name"],
                row["player_name"],
                row["role"],
                row["country"],
                row["num_bids"],
                row["last_bid_raw"],
                row["last_bid_cr"],
                row["bid_war"],
                row["source"],
                row["num_bids"],
                row["last_bid_raw"],
                row["bid_war"],
            ),
        )

    conn.commit()
    conn.close()
    print(f"Imported {len(rows)} rows into {db_path} (bid_history)")


def main():
    parser = argparse.ArgumentParser(description="Scrape Cricbuzz See All Bids per team/year")
    parser.add_argument(
        "--years",
        type=str,
        default="",
        help="Explicit list, e.g. 2018,2019,2026 (overrides --from-year/--to-year)",
    )
    parser.add_argument(
        "--from-year",
        type=int,
        default=2018,
        help="First IPL auction year to scrape (default: 2018)",
    )
    parser.add_argument(
        "--to-year",
        type=int,
        default=2026,
        help="Last IPL auction year to scrape (default: 2026)",
    )
    parser.add_argument(
        "--teams",
        type=str,
        default="",
        help="Comma team codes to limit, e.g. CSK,MI (default: all on page)",
    )
    parser.add_argument("--delay", type=float, default=2.0, help="Seconds between years")
    parser.add_argument("--import-db", action="store_true")
    parser.add_argument("--out", type=str, default=str(OUT_CSV))
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run headless (often fails on Cricbuzz Teams page — default is visible Chrome)",
    )
    parser.add_argument("--browser", choices=("chromium", "chrome"), default="chrome")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Install: pip install playwright && playwright install chromium")
        return

    team_filter = None
    if args.teams.strip():
        team_filter = {t.strip().upper() for t in args.teams.split(",")}

    debug_dir = ROOT / "data" / "scrape_debug" if args.debug else None

    print("Cricbuzz — Teams → See All Bids (team-wise storage)\n")
    print(f"Hub: {AUCTION_HUB_URL}\n")

    all_rows: List[Dict] = []
    with sync_playwright() as p:
        launch_kw = {
            "headless": args.headless,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if args.browser == "chrome":
            try:
                browser = p.chromium.launch(channel="chrome", **launch_kw)
            except Exception:
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
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        page = context.new_page()

        if args.headless:
            print(
                "WARNING: headless mode often shows h1='Cricket Teams' with 0 franchises.\n"
                "If scrape fails, re-run WITHOUT --headless (visible Chrome window).\n"
            )
        else:
            print("Using visible Chrome window (required for Cricbuzz auction SPA).\n")

        years: List[int] = []
        if args.years.strip():
            years = [int(y.strip()) for y in args.years.split(",")]
        else:
            years = list(range(args.from_year, args.to_year + 1))

        print(f"Years: {years[0]}–{years[-1]} ({len(years)} seasons) | all teams")
        print(f"Team-wise CSV: {OUT_DIR}/{{year}}/{{TEAM}}.csv\n")

        out_dir = OUT_DIR
        out_dir.mkdir(parents=True, exist_ok=True)

        for year in years:
            all_rows.extend(
                scrape_year(page, year, team_filter, out_dir, debug_dir)
            )
            time.sleep(args.delay)

        browser.close()

    if not all_rows:
        print("\nNo bid rows scraped.")
        print("Try: python scripts/scrape_cricbuzz_all_bids.py --years 2026 --teams CSK --browser chrome")
        return

    by_year: Dict[int, int] = {}
    for r in all_rows:
        by_year[r["year"]] = by_year.get(r["year"], 0) + 1
    print("\nRows per year:")
    for y in sorted(by_year):
        team_files = list((OUT_DIR / str(y)).glob("*.csv")) if (OUT_DIR / str(y)).exists() else []
        print(f"  {y}: {by_year[y]} rows | {len(team_files)} team files in {OUT_DIR}/{y}/")

    save_csv(all_rows, Path(args.out))
    if args.import_db:
        import_to_db(all_rows, DB_PATH)

    won = sum(1 for r in all_rows if r["bid_war"] == "won")
    print(f"\nDone. Rows: {len(all_rows)} | Won: {won} | Lost: {len(all_rows) - won}")


if __name__ == "__main__":
    main()
