"""
ESPN Cricinfo portraits — search, uniform square CDN URLs, SQLite mapping.

Stores per-player ESPN ids in player_espn_cricinfo and caches image bytes via
player_portrait_store (source=espncricinfo).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.getenv("DB_PATH", os.path.join(ROOT, "auction_data.db"))

ESPN_TRANSFORM = os.getenv(
    "ESPN_PORTRAIT_TRANSFORM",
    "f_auto,t_ds_square_w_400,q_85",
).strip()

ESPN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.espncricinfo.com/",
}

CONSUMER_SEARCH = "https://hs-consumer-api.espncricinfo.com/v1/pages/player/search"
CONSUMER_HOME = "https://hs-consumer-api.espncricinfo.com/v1/pages/player/home"
PROFILE_BASE = "https://www.espncricinfo.com/cricketers/"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS player_espn_cricinfo (
    player_key          TEXT PRIMARY KEY,
    display_name        TEXT NOT NULL,
    espn_player_id      TEXT NOT NULL,
    espn_slug           TEXT,
    raw_image_url       TEXT,
    uniform_image_url   TEXT NOT NULL,
    updated_at          TEXT NOT NULL
)
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _normalize_key(name: str) -> str:
    s = unicodedata.normalize("NFKD", (name or "").strip().lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s or "player"


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s)
        if not unicodedata.combining(c)
    )


def _name_match(search: str, candidate: str) -> bool:
    if not search or not candidate:
        return False
    a = _normalize_key(search)
    b = _normalize_key(candidate)
    if a == b:
        return True
    pa = _normalize_key(_strip_accents(search))
    pb = _normalize_key(_strip_accents(candidate))
    if pa == pb:
        return True
    sa, sb = a.split(), b.split()
    if len(sa) >= 2 and len(sb) >= 2 and sa[-1] == sb[-1] and sa[0] == sb[0]:
        return True
    if len(sa) >= 2 and len(sb) >= 1 and sa[-1] == sb[-1]:
        if len(sb[0]) <= 2 and sa[0].startswith(sb[0][0]):
            return True
    return False


def ensure_espn_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(auction_prices_full)").fetchall()}
    if "espn_cricinfo_id" not in cols:
        try:
            conn.execute("ALTER TABLE auction_prices_full ADD COLUMN espn_cricinfo_id TEXT")
        except sqlite3.OperationalError:
            pass
    conn.commit()


def uniform_portrait_url(raw: str) -> str:
    """Apply one Cloudinary transform to any ESPN hscicdn path."""
    if not raw or not str(raw).strip():
        return ""
    s = str(raw).strip()
    if s.startswith("//"):
        s = "https:" + s
    path_m = re.search(
        r"(lsci/(?:db/)?PICTURES/[^\s?\"'<>]+|lsci/players/[^\s?\"'<>]+)",
        s,
        re.IGNORECASE,
    )
    if not path_m:
        return s if s.startswith("http") else ""
    path = path_m.group(1)
    return f"https://img1.hscicdn.com/image/upload/{ESPN_TRANSFORM}/{path}"


def _pick_image_url(obj: Dict[str, Any]) -> Optional[str]:
    for key in (
        "headshotImageUrl",
        "headshot_image_url",
        "imageUrl",
        "image_url",
        "playerImageUrl",
        "player_image_url",
        "image",
    ):
        val = obj.get(key)
        if isinstance(val, str) and "hscicdn" in val:
            return val
        if isinstance(val, dict):
            u = val.get("url") or val.get("src")
            if isinstance(u, str) and "hscicdn" in u:
                return u
    return None


def _player_id(obj: Dict[str, Any]) -> Optional[str]:
    for key in ("objectId", "id", "playerId", "player_id"):
        val = obj.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return None


def _player_name(obj: Dict[str, Any]) -> str:
    for key in ("longName", "long_name", "name", "fullName", "title", "playerName"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _player_slug(obj: Dict[str, Any]) -> str:
    for key in ("slug", "playerSlug"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _collect_player_dicts(obj: Any, out: List[Dict[str, Any]], depth: int = 0) -> None:
    if depth > 14:
        return
    if isinstance(obj, dict):
        pid = _player_id(obj)
        name = _player_name(obj)
        img = _pick_image_url(obj)
        if pid and (name or img):
            out.append(obj)
        for v in obj.values():
            _collect_player_dicts(v, out, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _collect_player_dicts(item, out, depth + 1)


def _best_search_hit(display_name: str, candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    resolved = display_name.strip()
    best: Optional[Dict[str, Any]] = None
    best_score = -1.0
    for row in candidates:
        name = _player_name(row)
        if not name:
            continue
        if not _name_match(resolved, name):
            continue
        score = 100.0
        if _normalize_key(name) == _normalize_key(resolved):
            score += 50
        sport = str(row.get("sport") or row.get("category") or "").lower()
        if "cricket" in sport or not sport:
            score += 10
        if _pick_image_url(row):
            score += 5
        if score > best_score:
            best_score = score
            best = row
    return best


def _parse_next_data_player(html: str) -> Optional[Dict[str, Any]]:
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return None
    try:
        payload = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    found: List[Dict[str, Any]] = []
    _collect_player_dicts(payload, found)
    if not found:
        return None
    # Prefer dict with headshot / CMS image
    for row in found:
        if _pick_image_url(row) and _player_id(row):
            return row
    return found[0] if found else None


def _profile_url_from_parts(slug: str, player_id: str) -> str:
    slug_s = (slug or "player").strip().lower().replace(" ", "-")
    pid = str(player_id).strip()
    return f"{PROFILE_BASE}{slug_s}-{pid}"


def lookup_espn_mapping(
    conn: sqlite3.Connection, player_key: str,
) -> Optional[Tuple[str, str, str]]:
    """Returns (espn_player_id, uniform_image_url, raw_image_url)."""
    ensure_espn_schema(conn)
    row = conn.execute(
        """
        SELECT espn_player_id, uniform_image_url, raw_image_url
        FROM player_espn_cricinfo WHERE player_key = ?
        """,
        (player_key,),
    ).fetchone()
    if not row:
        return None
    return str(row[0]), str(row[1]), str(row[2] or "")


def save_espn_mapping(
    conn: sqlite3.Connection,
    *,
    player_key: str,
    display_name: str,
    espn_player_id: str,
    espn_slug: str,
    raw_image_url: str,
    uniform_image_url: str,
) -> None:
    ensure_espn_schema(conn)
    conn.execute(
        """
        INSERT OR REPLACE INTO player_espn_cricinfo
        (player_key, display_name, espn_player_id, espn_slug, raw_image_url,
         uniform_image_url, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            player_key,
            display_name.strip(),
            str(espn_player_id).strip(),
            (espn_slug or "").strip() or None,
            (raw_image_url or "").strip() or None,
            uniform_image_url.strip(),
            _now(),
        ),
    )
    conn.execute(
        """
        UPDATE auction_prices_full
        SET espn_cricinfo_id = ?
        WHERE year = 2026 AND player_name = ?
        """,
        (str(espn_player_id).strip(), display_name.strip()),
    )
    conn.commit()


def _consumer_search(
    client: httpx.Client, query: str,
) -> List[Dict[str, Any]]:
    param_sets = [
        {"searchText": query, "records": 12, "page": 1},
        {"mode": "PREFIX", "searchText": query, "records": 12, "page": 1},
        {"mode": "prefix", "searchText": query, "records": 12, "page": 1},
        {"q": query, "limit": 12},
    ]
    found: List[Dict[str, Any]] = []
    for params in param_sets:
        try:
            resp = client.get(
                CONSUMER_SEARCH,
                params=params,
                headers=ESPN_HEADERS,
                timeout=12.0,
            )
            if resp.status_code >= 400:
                continue
            data = resp.json()
            batch: List[Dict[str, Any]] = []
            _collect_player_dicts(data, batch)
            found.extend(batch)
            if found:
                break
        except Exception as exc:
            logger.debug("ESPN search failed (%s): %s", params, exc)
    return found


def _consumer_home(client: httpx.Client, player_id: str) -> Optional[Dict[str, Any]]:
    try:
        resp = client.get(
            CONSUMER_HOME,
            params={"playerId": player_id, "lang": "en"},
            headers=ESPN_HEADERS,
            timeout=12.0,
        )
        if resp.status_code >= 400:
            return None
        data = resp.json()
        batch: List[Dict[str, Any]] = []
        _collect_player_dicts(data, batch)
        for row in batch:
            if _player_id(row) == str(player_id) and _pick_image_url(row):
                return row
        return batch[0] if batch else None
    except Exception as exc:
        logger.debug("ESPN player/home failed for %s: %s", player_id, exc)
        return None


def _cricketer_links_from_html(html: str) -> List[Tuple[str, str, str]]:
    """Returns list of (path, slug, player_id)."""
    patterns = [
        r'href="(/cricketers/([a-z0-9-]+)-(\d+))"',
        r'href="(https://www\.espncricinfo\.com/cricketers/([a-z0-9-]+)-(\d+))"',
    ]
    seen: set = set()
    out: List[Tuple[str, str, str]] = []
    for pat in patterns:
        for m in re.finditer(pat, html, re.IGNORECASE):
            path, slug, pid = m.group(1), m.group(2), m.group(3)
            key = (slug, pid)
            if key in seen:
                continue
            seen.add(key)
            out.append((path, slug, pid))
    return out


def _search_via_html(client: httpx.Client, display_name: str) -> Optional[Dict[str, Any]]:
    """Site search pages when consumer API is unavailable."""
    urls = [
        f"https://www.espncricinfo.com/search/results?q={quote(display_name)}",
        f"https://www.espncricinfo.com/ci/content/player/search.html?search={quote(display_name)}",
    ]
    links: List[Tuple[str, str, str]] = []
    for url in urls:
        try:
            resp = client.get(url, headers=ESPN_HEADERS, timeout=18.0)
            if resp.status_code >= 400 or not resp.text:
                continue
            links.extend(_cricketer_links_from_html(resp.text))
            parsed = _parse_next_data_player(resp.text)
            if parsed and _player_id(parsed):
                name = _player_name(parsed) or display_name
                if _name_match(display_name, name):
                    return parsed
        except Exception as exc:
            logger.debug("ESPN HTML search page failed %s: %s", url, exc)

    for _path, slug, pid in links[:12]:
        probe = _scrape_profile_page(client, slug, pid)
        if not probe:
            continue
        pname = _player_name(probe) or display_name
        if _name_match(display_name, pname):
            return probe
    return None


def _scrape_profile_page(
    client: httpx.Client, slug: str, player_id: str,
) -> Optional[Dict[str, Any]]:
    url = _profile_url_from_parts(slug, player_id)
    try:
        resp = client.get(url, headers=ESPN_HEADERS, timeout=15.0)
        if resp.status_code >= 400:
            return None
        html = resp.text
    except Exception as exc:
        logger.debug("ESPN profile fetch failed %s: %s", url, exc)
        return None

    row = _parse_next_data_player(html)
    if row:
        if not _player_id(row):
            row = {**row, "objectId": player_id}
        if not _player_slug(row):
            row = {**row, "slug": slug}
        img = _pick_image_url(row)
        if img:
            return row

    for raw_url in re.findall(
        r"https://img1\.hscicdn\.com/image/upload/[^\s\"'<>]+",
        html,
    ):
        if "PICTURES" in raw_url or "players/" in raw_url:
            return {
                "objectId": player_id,
                "slug": slug,
                "imageUrl": raw_url,
            }
    og = re.search(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    )
    if og and "hscicdn" in og.group(1):
        return {"objectId": player_id, "slug": slug, "imageUrl": og.group(1)}
    return None


def resolve_espn_player(
    client: httpx.Client,
    display_name: str,
    *,
    espn_player_id: Optional[str] = None,
) -> Optional[Dict[str, str]]:
    """
    Resolve ESPN player id + uniform portrait URL for a display name.
    Returns dict with keys: espn_player_id, espn_slug, raw_image_url, uniform_image_url.
    """
    clean = display_name.strip()
    if not clean:
        return None

    row: Optional[Dict[str, Any]] = None
    if espn_player_id:
        row = _consumer_home(client, espn_player_id)
        if not row or not _pick_image_url(row):
            row = _scrape_profile_page(client, "player", espn_player_id)

    if not row:
        hits = _consumer_search(client, clean)
        row = _best_search_hit(clean, hits)

    if not row:
        row = _search_via_html(client, clean)

    if not row:
        return None

    pid = _player_id(row)
    if not pid:
        return None
    slug = _player_slug(row) or "player"
    raw = _pick_image_url(row) or ""
    if not raw:
        home = _consumer_home(client, pid)
        if home:
            raw = _pick_image_url(home) or ""
    if not raw:
        scraped = _scrape_profile_page(client, slug, pid)
        if scraped:
            raw = _pick_image_url(scraped) or ""

    if not raw:
        return None

    uniform = uniform_portrait_url(raw)
    if not uniform:
        return None

    return {
        "espn_player_id": pid,
        "espn_slug": slug,
        "raw_image_url": raw,
        "uniform_image_url": uniform,
    }


def download_espn_portrait(
    client: httpx.Client, uniform_url: str,
) -> Optional[Tuple[bytes, str]]:
    from player_portrait_store import _download_image

    return _download_image(client, uniform_url, headers=ESPN_HEADERS)


def fetch_and_store_espn_portrait(
    conn: sqlite3.Connection,
    client: httpx.Client,
    display_name: str,
    *,
    force: bool = False,
) -> Optional[Tuple[bytes, str, str]]:
    """
    Resolve ESPN portrait, save mapping + player_portraits row.
    Returns (image_bytes, content_type, source) or None.
    """
    from player_portrait_store import (
        _cache_portrait,
        _clear_portrait_entry,
        _normalize_key as pk_norm,
        _read_cache,
    )

    clean = display_name.strip()
    player_key = pk_norm(clean)
    if not force:
        cached = _read_cache(conn, player_key)
        if cached and cached[2] == "espncricinfo":
            return cached

    mapping = lookup_espn_mapping(conn, player_key)
    uniform_url = mapping[1] if mapping else None
    espn_id = mapping[0] if mapping else None

    if not uniform_url:
        resolved = resolve_espn_player(client, clean, espn_player_id=espn_id)
        if not resolved:
            return None
        save_espn_mapping(
            conn,
            player_key=player_key,
            display_name=clean,
            espn_player_id=resolved["espn_player_id"],
            espn_slug=resolved["espn_slug"],
            raw_image_url=resolved["raw_image_url"],
            uniform_image_url=resolved["uniform_image_url"],
        )
        uniform_url = resolved["uniform_image_url"]
        espn_id = resolved["espn_player_id"]

    got = download_espn_portrait(client, uniform_url)
    if not got:
        return None

    if force:
        _clear_portrait_entry(conn, player_key)

    cached = _cache_portrait(
        conn,
        player_key=player_key,
        display_name=clean,
        source="espncricinfo",
        image_url=uniform_url,
        content_type=got[1],
        image_data=got[0],
    )
    return cached
