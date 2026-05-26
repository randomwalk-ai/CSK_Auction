"""
IPL official face-card URLs (documents.iplt20.com) — URL only, no image bytes.

Index built from iplt20.com team squad pages; stored per auction player_key.
"""

from __future__ import annotations

import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from player_portrait_store import _normalize_key

ROOT_TEAM_SLUGS = [
    "chennai-super-kings",
    "mumbai-indians",
    "royal-challengers-bengaluru",
    "kolkata-knight-riders",
    "delhi-capitals",
    "punjab-kings",
    "rajasthan-royals",
    "sunrisers-hyderabad",
    "gujarat-titans",
    "lucknow-super-giants",
]

HEADSHOT_YEARS = [2026, 2025, 2024, 2023]

IPL_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS player_ipl_facecards (
    player_key          TEXT PRIMARY KEY,
    display_name        TEXT NOT NULL,
    ipl_player_id       TEXT NOT NULL,
    facecard_url        TEXT NOT NULL,
    facecard_year       INTEGER,
    matched_ipl_name    TEXT,
    team_slug           TEXT,
    updated_at          TEXT NOT NULL
)
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def normalize_ipl_name(name: str) -> str:
    """Lowercase, strip accents/punctuation, collapse whitespace."""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-zA-Z ]+", " ", s).lower()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def ensure_ipl_facecard_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)
    conn.commit()


def build_squad_index(client: httpx.Client) -> Dict[str, Dict[str, str]]:
    """Scrape all team squad pages -> {normalized_name: {id, display_name, team}}."""
    index: Dict[str, Dict[str, str]] = {}
    for slug in ROOT_TEAM_SLUGS:
        url = f"https://www.iplt20.com/teams/{slug}/squad"
        try:
            resp = client.get(url, headers=IPL_UA, timeout=20.0)
            resp.raise_for_status()
        except Exception:
            continue

        html = resp.text
        for m in re.finditer(r"IPLHeadshot\d+/(\d+)\.png", html):
            pid = m.group(1)
            tail = html[m.end(): m.end() + 600]
            name_match = re.search(r">([A-Za-z][A-Za-z.\' -]{2,40})<", tail)
            if not name_match:
                continue
            name = name_match.group(1).strip()
            key = normalize_ipl_name(name)
            if key and key not in index:
                index[key] = {
                    "id": pid,
                    "display_name": name,
                    "team": slug,
                }
        time.sleep(0.3)
    return index


def lookup_in_squad_index(name: str, index: Dict[str, Dict[str, str]]) -> Optional[Dict[str, str]]:
    key = normalize_ipl_name(name)
    if key in index:
        return index[key]

    parts = key.split()
    if len(parts) >= 2:
        first_initial = parts[0][0]
        surname = parts[-1]
        for k, v in index.items():
            kp = k.split()
            if kp and kp[-1] == surname and kp[0].startswith(first_initial):
                return v
        candidates = [v for k, v in index.items() if k.split()[-1] == surname]
        if len(candidates) == 1:
            return candidates[0]

    for k, v in index.items():
        if key in k or k in key:
            return v
    return None


def find_working_facecard_url(
    client: httpx.Client, player_id: str,
) -> Tuple[Optional[str], Optional[int]]:
    """Return (url, year) for the newest year that returns HTTP 200 image."""
    for year in HEADSHOT_YEARS:
        url = f"https://documents.iplt20.com/ipl/IPLHeadshot{year}/{player_id}.png"
        try:
            resp = client.head(url, headers=IPL_UA, timeout=10.0, follow_redirects=True)
            ct = (resp.headers.get("content-type") or "").lower()
            if resp.status_code == 200 and ct.startswith("image"):
                return url, year
        except Exception:
            continue
    return None, None


def save_facecard_row(
    conn: sqlite3.Connection,
    *,
    player_key: str,
    display_name: str,
    ipl_player_id: str,
    facecard_url: str,
    facecard_year: Optional[int],
    matched_ipl_name: str,
    team_slug: str,
) -> None:
    ensure_ipl_facecard_schema(conn)
    conn.execute(
        """
        INSERT OR REPLACE INTO player_ipl_facecards
        (player_key, display_name, ipl_player_id, facecard_url, facecard_year,
         matched_ipl_name, team_slug, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            player_key,
            display_name.strip(),
            str(ipl_player_id).strip(),
            facecard_url.strip(),
            facecard_year,
            (matched_ipl_name or "").strip() or None,
            (team_slug or "").strip() or None,
            _now(),
        ),
    )
    conn.commit()


def lookup_facecard(
    conn: sqlite3.Connection, display_name: str,
) -> Optional[Dict[str, Any]]:
    ensure_ipl_facecard_schema(conn)
    key = _normalize_key(display_name)
    row = conn.execute(
        """
        SELECT display_name, ipl_player_id, facecard_url, facecard_year,
               matched_ipl_name, team_slug
        FROM player_ipl_facecards WHERE player_key = ?
        """,
        (key,),
    ).fetchone()
    if not row:
        return None
    return {
        "display_name": row[0],
        "ipl_player_id": row[1],
        "facecard_url": row[2],
        "facecard_year": row[3],
        "matched_ipl_name": row[4],
        "team_slug": row[5],
    }


def facecard_url_map(conn: sqlite3.Connection, display_names: List[str]) -> Dict[str, str]:
    """Map player_key -> facecard_url for a list of auction display names."""
    if not display_names:
        return {}
    ensure_ipl_facecard_schema(conn)
    keys = {_normalize_key(n) for n in display_names if n and str(n).strip()}
    if not keys:
        return {}
    placeholders = ",".join("?" * len(keys))
    rows = conn.execute(
        f"""
        SELECT player_key, facecard_url
        FROM player_ipl_facecards
        WHERE player_key IN ({placeholders})
          AND facecard_url IS NOT NULL AND TRIM(facecard_url) != ''
        """,
        tuple(keys),
    ).fetchall()
    return {str(r[0]): str(r[1]) for r in rows}
