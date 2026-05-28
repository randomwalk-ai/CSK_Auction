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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.getenv("DB_PATH", os.path.join(ROOT, "auction_data.db"))
_ESPN_COOKIES_FILE_DEFAULT = Path(ROOT) / ".espn_cookies"


def _espn_cookie_string() -> str:
    """ESPN_COOKIES env, or ESPN_COOKIES_FILE, or gitignored .espn_cookies in project root."""
    raw = os.getenv("ESPN_COOKIES", "").strip()
    if raw:
        return raw
    path_s = os.getenv("ESPN_COOKIES_FILE", "").strip()
    paths = [Path(path_s)] if path_s else [_ESPN_COOKIES_FILE_DEFAULT]
    for path in paths:
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return ""

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
    "Origin": "https://www.espncricinfo.com",
}

_HSCICDN_PORTRAIT_RE = re.compile(
    r"https?:\\?/\\?/img1\.hscicdn\.com/image/upload/[^\s\"'<>\\]+",
    re.IGNORECASE,
)

CONSUMER_SEARCH = "https://hs-consumer-api.espncricinfo.com/v1/pages/player/search"
CONSUMER_HOME = "https://hs-consumer-api.espncricinfo.com/v1/pages/player/home"
CONSUMER_TEAM_SQUAD = "https://hs-consumer-api.espncricinfo.com/v1/pages/team/squad"
CONSUMER_SERIES_SQUADS = "https://hs-consumer-api.espncricinfo.com/v1/pages/series/squads"
LEGACY_PLAYER_SEARCH = "https://www.espncricinfo.com/ci/content/player/search.html"
PROFILE_BASE = "https://www.espncricinfo.com/cricketers/"

IPL_2026_SERIES_ID = os.getenv("ESPN_IPL_2026_SERIES_ID", "1510719").strip()

# Verified ESPN franchise IDs for IPL 2026 (fallback when series /teams HTML has no links)
ESPN_IPL_TEAMS_2026: List[Tuple[str, List[str]]] = [
    ("royal-challengers-bengaluru", ["335970"]),
    ("kolkata-knight-riders", ["335971"]),
    ("punjab-kings", ["335972"]),
    ("rajasthan-royals", ["335973"]),
    ("chennai-super-kings", ["335974"]),
    ("delhi-capitals", ["335975"]),
    ("sunrisers-hyderabad", ["335976"]),
    ("gujarat-titans", ["335977", "1298769"]),
    ("mumbai-indians", ["335978"]),
    ("lucknow-super-giants", ["335979"]),
]

IPL_SERIES_TEAM_PAGES = [
    f"https://www.espncricinfo.com/series/ipl-2026-{IPL_2026_SERIES_ID}/teams",
    "https://www.espncricinfo.com/series/ipl-2025-1449924/teams",
]

_AUTH_TOKEN_CACHE: Tuple[str, float] = ("", 0.0)
_AUTH_TTL_SEC = 240.0
_ROSTER_INDEX: Dict[str, Dict[str, str]] = {}
_ROSTER_INDEX_BUILT = False

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
        if len(sb) == 1 and sa[-1] == sb[0]:
            return True
    if len(sa) >= 2 and len(sb) >= 2 and sa[-1] == sb[-1]:
        if sa[0][:1] == sb[0][:1]:
            return True
    return False


def _use_playwright() -> bool:
    return os.getenv("ESPN_USE_PLAYWRIGHT", "0").strip().lower() in ("1", "true", "yes")


def _httpx_cookies_from_env() -> httpx.Cookies:
    """Parse ESPN_COOKIES env (browser document.cookie export)."""
    jar = httpx.Cookies()
    raw = _espn_cookie_string()
    if not raw:
        return jar
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, val = part.partition("=")
        key = key.strip()
        if not key:
            continue
        jar.set(key, val.strip(), domain=".espncricinfo.com")
        jar.set(key, val.strip(), domain="www.espncricinfo.com")
    return jar


def _espn_document_headers(*, referer: Optional[str] = None) -> Dict[str, str]:
    """HTML/navigation requests — cookies only (no consumer auth token)."""
    headers = dict(ESPN_HEADERS)
    headers["Accept"] = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"
    headers["Sec-Fetch-Dest"] = "document"
    headers["Sec-Fetch-Mode"] = "navigate"
    headers["Sec-Fetch-Site"] = "same-origin"
    cookie = _espn_cookie_string()
    if cookie:
        headers["Cookie"] = cookie
    if referer:
        headers["Referer"] = referer
    return headers


def _espn_browser_headers(
    client: Optional[httpx.Client] = None, *, json_api: bool = False,
) -> Dict[str, str]:
    """Browser-like headers; consumer API adds x-hsci-auth-token when json_api=True."""
    if not json_api:
        return _espn_document_headers()
    headers = _espn_document_headers()
    headers["Accept"] = "application/json, text/plain, */*"
    headers["Sec-Fetch-Dest"] = "empty"
    headers["Sec-Fetch-Mode"] = "cors"
    headers["Sec-Fetch-Site"] = "same-site"
    if client is not None:
        token = _consumer_auth_token(client)
        if token:
            headers["x-hsci-auth-token"] = token
    return headers


def _html_has_next_data(html: str) -> bool:
    return "__NEXT_DATA__" in (html or "")


def _playwright_auto() -> bool:
    """Use headless browser for profiles when cookies are set but httpx HTML is empty."""
    if not _espn_cookie_string():
        return False
    return os.getenv("ESPN_PLAYWRIGHT_AUTO", "1").strip().lower() in ("1", "true", "yes")


def _auth_token_from_html(html: str) -> str:
    for pat in (
        r'x-hsci-auth-token["\']?\s*[:=]\s*["\']([^"\']+)',
        r'"authToken"\s*:\s*"([^"]+)"',
        r'"x-hsci-auth-token"\s*:\s*"([^"]+)"',
        r'"hsciAuthToken"\s*:\s*"([^"]+)"',
    ):
        m = re.search(pat, html or "", re.IGNORECASE)
        if m and len(m.group(1)) > 12:
            return m.group(1).strip()
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html or "",
        re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return ""
    try:
        payload = json.loads(m.group(1))
    except json.JSONDecodeError:
        return ""

    stack: List[Any] = [payload]
    while stack:
        obj = stack.pop()
        if isinstance(obj, dict):
            for key, val in obj.items():
                lk = str(key).lower()
                if lk in ("authtoken", "x-hsci-auth-token", "hsciauthtoken") and isinstance(val, str):
                    if len(val) > 12:
                        return val.strip()
                if isinstance(val, (dict, list)):
                    stack.append(val)
        elif isinstance(obj, list):
            stack.extend(obj)
    return ""


def _cache_auth_token_from_response(resp: httpx.Response) -> None:
    global _AUTH_TOKEN_CACHE
    token = (resp.headers.get("x-hsci-auth-token") or "").strip()
    if not token:
        token = _auth_token_from_html(resp.text or "")
    if token:
        _AUTH_TOKEN_CACHE = (token, time.time())


def _normalize_media_text(text: str) -> str:
    return (
        (text or "")
        .replace("\\/", "/")
        .replace("\\u002F", "/")
        .replace("\\u002f", "/")
    )


def _is_hscicdn_portrait_url(url: str) -> bool:
    clean = _normalize_media_text(url).replace("\\", "")
    if "hscicdn" not in clean or "/image/upload/" not in clean:
        return False
    return "PICTURES" in clean or "players/" in clean or "/lsci/" in clean


def _find_hscicdn_portrait_url(text: str) -> Optional[str]:
    """First ESPN headshot URL in HTML or __NEXT_DATA__ JSON."""
    norm = _normalize_media_text(text)
    for url in _HSCICDN_PORTRAIT_RE.findall(norm):
        clean = url.replace("\\", "")
        if _is_hscicdn_portrait_url(clean):
            return clean
    return None


def _map_player_ids_to_portraits(text: str) -> Dict[str, str]:
    """objectId/playerId -> hscicdn portrait URL from embedded JSON in HTML."""
    norm = _normalize_media_text(text)
    out: Dict[str, str] = {}
    patterns = (
        r'"objectId"\s*:\s*"?(\d+)"?[\s\S]{0,5000}?(https?://img1\.hscicdn\.com/image/upload/[^"\\]+)',
        r'"playerId"\s*:\s*"?(\d+)"?[\s\S]{0,5000}?(https?://img1\.hscicdn\.com/image/upload/[^"\\]+)',
        r'"id"\s*:\s*"?(\d+)"?[\s\S]{0,3000}?"imageUrl"\s*:\s*"(https?://img1\.hscicdn\.com/image/upload/[^"\\]+)"',
        r'/cricketers/[a-z0-9-]+-(\d+)[\s\S]{0,3000}?(https?://img1\.hscicdn\.com/image/upload/[^"\\]+)',
    )
    for pat in patterns:
        for m in re.finditer(pat, norm, re.IGNORECASE):
            pid, url = m.group(1), m.group(2).replace("\\", "")
            if _is_hscicdn_portrait_url(url):
                out.setdefault(str(pid), url)
    return out


def _patch_roster_portraits_from_html(html: str, roster: Dict[str, Dict[str, str]]) -> None:
    for pid, url in _map_player_ids_to_portraits(html).items():
        for entry in roster.values():
            if str(entry.get("espn_player_id")) == str(pid) and not entry.get("raw_image_url"):
                entry["raw_image_url"] = url


def _legacy_search_html(client: httpx.Client, display_name: str) -> str:
    pages: List[str] = []
    for url, params in (
        (LEGACY_PLAYER_SEARCH, {"search": display_name}),
        (
            "https://www.espncricinfo.com/ci/engine/player/index.html",
            {"search": display_name},
        ),
    ):
        try:
            resp = client.get(url, params=params, headers=_espn_document_headers(), timeout=20.0)
            if resp.status_code < 400 and resp.text:
                pages.append(resp.text)
        except Exception as exc:
            logger.debug("ESPN legacy search fetch failed %s: %s", url, exc)
    return "\n".join(pages)


def _portrait_row(slug: str, player_id: str, image_url: str, name: str = "") -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "objectId": player_id,
        "slug": slug,
        "imageUrl": image_url,
    }
    if name:
        row["longName"] = name
    return row


def _deep_hscicdn_in_obj(obj: Any) -> Optional[str]:
    """Walk JSON for any ESPN headshot CDN string."""
    if isinstance(obj, str):
        if "hscicdn" in obj and ("PICTURES" in obj or "players/" in obj):
            found = _find_hscicdn_portrait_url(obj)
            return found
        return None
    if isinstance(obj, dict):
        for val in obj.values():
            hit = _deep_hscicdn_in_obj(val)
            if hit:
                return hit
    elif isinstance(obj, list):
        for item in obj:
            hit = _deep_hscicdn_in_obj(item)
            if hit:
                return hit
    return None


def _extract_ci_display_name(label: str) -> str:
    """'Curran, SM (Sam Curran, 1998- )' -> 'Sam Curran'."""
    text = (label or "").strip()
    m = re.search(r"\(([^,0-9][^,)]*)", text)
    if m:
        return m.group(1).strip()
    if "," in text:
        return text.split(",", 1)[0].strip()
    return text


def _consumer_auth_token(client: httpx.Client) -> str:
    global _AUTH_TOKEN_CACHE
    cached, ts = _AUTH_TOKEN_CACHE
    if cached and (time.time() - ts) < _AUTH_TTL_SEC:
        return cached

    token = ""
    bootstrap_urls = (
        "https://www.espncricinfo.com/",
        "https://www.espncricinfo.com/live-cricket-score",
        LEGACY_PLAYER_SEARCH,
    )
    for url in bootstrap_urls:
        try:
            resp = client.get(url, headers=_espn_document_headers(), timeout=20.0)
            _cache_auth_token_from_response(resp)
            token = (resp.headers.get("x-hsci-auth-token") or "").strip()
            if not token:
                token = _auth_token_from_html(resp.text or "")
            if token:
                break
        except Exception as exc:
            logger.debug("ESPN auth bootstrap failed %s: %s", url, exc)

    _AUTH_TOKEN_CACHE = (token, time.time())
    if token:
        logger.debug("ESPN consumer auth token acquired (%s chars)", len(token))
    return token


def _consumer_request_headers(client: httpx.Client) -> Dict[str, str]:
    return _espn_browser_headers(client, json_api=True)


def _row_to_resolved(row: Dict[str, Any]) -> Optional[Dict[str, str]]:
    pid = _player_id(row)
    if not pid:
        return None
    slug = _player_slug(row) or "player"
    raw = _pick_image_url(row) or ""
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


def _lookup_auction_espn_id(
    conn: Optional[sqlite3.Connection], display_name: str,
) -> Optional[str]:
    if conn is None:
        return None
    try:
        row = conn.execute(
            """
            SELECT espn_cricinfo_id FROM auction_prices_full
            WHERE year = 2026 AND player_name = ?
              AND espn_cricinfo_id IS NOT NULL AND TRIM(espn_cricinfo_id) != ''
            LIMIT 1
            """,
            (display_name.strip(),),
        ).fetchone()
        if row and row[0]:
            return str(row[0]).strip()
    except sqlite3.Error:
        pass
    return None


def espn_http_client(**kwargs: Any) -> httpx.Client:
    """HTTP client for ESPN — trust_env off by default; inject ESPN_COOKIES when set."""
    trust = os.getenv("ESPN_HTTP_TRUST_ENV", "0").strip().lower() in ("1", "true", "yes")
    timeout = kwargs.pop("timeout", 25.0)
    cookies = kwargs.pop("cookies", None) or _httpx_cookies_from_env()
    return httpx.Client(
        follow_redirects=True,
        trust_env=trust,
        timeout=timeout,
        cookies=cookies,
        **kwargs,
    )


def _team_page_ok(client: httpx.Client, slug: str, team_id: str) -> bool:
    url = f"https://www.espncricinfo.com/team/{slug}-{team_id}"
    try:
        resp = client.get(url, headers=_espn_document_headers(), timeout=15.0)
        return resp.status_code < 400 and len(resp.text or "") > 2000
    except Exception:
        return False


def _resolve_team_id(client: httpx.Client, slug: str, candidates: List[str]) -> Optional[str]:
    for tid in candidates:
        if _team_page_ok(client, slug, tid):
            return tid
    return candidates[0] if candidates else None


def _discover_ipl_team_ids(client: httpx.Client) -> List[Tuple[str, str]]:
    """Return [(slug, team_object_id), ...] from series pages + hardcoded IPL 2026 list."""
    found: Dict[str, str] = {}
    for page_url in IPL_SERIES_TEAM_PAGES:
        try:
            resp = client.get(page_url, headers=_espn_document_headers(), timeout=20.0)
            if resp.status_code >= 400:
                continue
            for slug, tid in re.findall(
                r"/team/([a-z0-9-]+)-(\d+)", resp.text or "", re.IGNORECASE,
            ):
                found[slug.lower()] = tid
        except Exception as exc:
            logger.debug("ESPN team discovery failed %s: %s", page_url, exc)

    for slug, id_candidates in ESPN_IPL_TEAMS_2026:
        if slug in found:
            continue
        tid = _resolve_team_id(client, slug, id_candidates)
        if tid:
            found[slug] = tid

    if len(found) < 8:
        for slug, id_candidates in ESPN_IPL_TEAMS_2026:
            tid = _resolve_team_id(client, slug, id_candidates)
            if tid:
                found[slug] = tid

    return sorted(found.items())


def _ingest_players_from_html(html: str, roster: Dict[str, Dict[str, str]]) -> int:
    if not html:
        return 0
    before = len(roster)
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if m:
        try:
            batch: List[Dict[str, Any]] = []
            payload = json.loads(m.group(1))
            _collect_player_dicts(payload, batch)
            id_map = _map_player_ids_to_portraits(html)
            for row in batch:
                pid = _player_id(row)
                if pid and pid in id_map and not _pick_image_url(row):
                    row = {**row, "imageUrl": id_map[pid]}
                _ingest_player_into_roster(row, roster)
        except json.JSONDecodeError:
            pass
    _patch_roster_portraits_from_html(html, roster)
    return len(roster) - before


def _ipl_2026_squad_page(client: httpx.Client, slug: str, team_id: str) -> Optional[str]:
    squads_url = f"https://www.espncricinfo.com/team/{slug}-{team_id}/squads"
    try:
        resp = client.get(squads_url, headers=_espn_document_headers(), timeout=20.0)
        if resp.status_code >= 400:
            return None
        for rel in re.findall(
            r'href="(/series/ipl-2026-\d+/[a-z0-9-]+-squad-\d+/series-squads)"',
            resp.text or "",
            re.IGNORECASE,
        ):
            return f"https://www.espncricinfo.com{rel}"
    except Exception as exc:
        logger.debug("ESPN squads list failed %s: %s", squads_url, exc)
    return None


def _ingest_series_squads_consumer(
    client: httpx.Client, roster: Dict[str, Dict[str, str]], headers: Dict[str, str],
) -> None:
    for series_id in (IPL_2026_SERIES_ID, "1449924"):
        try:
            resp = client.get(
                CONSUMER_SERIES_SQUADS,
                params={"seriesId": series_id, "lang": "en"},
                headers=headers,
                timeout=22.0,
            )
            if resp.status_code >= 400:
                continue
            body = resp.text or ""
            batch: List[Dict[str, Any]] = []
            _collect_player_dicts(resp.json(), batch)
            id_map = _map_player_ids_to_portraits(body)
            for row in batch:
                pid = _player_id(row)
                if pid and pid in id_map and not _pick_image_url(row):
                    row = {**row, "imageUrl": id_map[pid]}
                _ingest_player_into_roster(row, roster)
            _patch_roster_portraits_from_html(body, roster)
        except Exception as exc:
            logger.debug("ESPN series squads API %s: %s", series_id, exc)


def _ingest_player_into_roster(row: Dict[str, Any], roster: Dict[str, Dict[str, str]]) -> None:
    pid = _player_id(row)
    name = _player_name(row)
    if not pid or not name:
        return
    slug = _player_slug(row) or "player"
    raw = _pick_image_url(row) or _deep_hscicdn_in_obj(row) or ""
    entry = {
        "espn_player_id": pid,
        "espn_slug": slug,
        "display_name": name,
        "raw_image_url": raw,
    }
    for key in {_normalize_key(name), _normalize_key(_extract_ci_display_name(name))}:
        if key and key not in roster:
            roster[key] = entry


def warm_espn_roster_index(client: httpx.Client, *, force: bool = False) -> int:
    """
    Build in-memory name -> ESPN player map from IPL 2026 squads (consumer + HTML).
    Call once before batch seeding.
    """
    global _ROSTER_INDEX, _ROSTER_INDEX_BUILT
    if _ROSTER_INDEX_BUILT and not force and _ROSTER_INDEX:
        return len(_ROSTER_INDEX)

    roster: Dict[str, Dict[str, str]] = {}
    headers = _consumer_request_headers(client)
    _ingest_series_squads_consumer(client, roster, headers)

    teams = _discover_ipl_team_ids(client)
    logger.info("ESPN IPL teams resolved: %s", len(teams))

    for slug, team_id in teams:
        try:
            resp = client.get(
                CONSUMER_TEAM_SQUAD,
                params={"teamId": team_id, "lang": "en"},
                headers=headers,
                timeout=18.0,
            )
            if resp.status_code < 400:
                body = resp.text or ""
                batch: List[Dict[str, Any]] = []
                _collect_player_dicts(resp.json(), batch)
                id_map = _map_player_ids_to_portraits(body)
                for row in batch:
                    pid = _player_id(row)
                    if pid and pid in id_map and not _pick_image_url(row):
                        row = {**row, "imageUrl": id_map[pid]}
                    _ingest_player_into_roster(row, roster)
                _patch_roster_portraits_from_html(body, roster)
        except Exception as exc:
            logger.debug("ESPN team squad API %s: %s", team_id, exc)

        for page_url in (
            _ipl_2026_squad_page(client, slug, team_id),
            f"https://www.espncricinfo.com/team/{slug}-{team_id}",
            f"https://www.espncricinfo.com/team/{slug}-{team_id}/squad",
        ):
            if not page_url:
                continue
            try:
                resp = client.get(page_url, headers=_espn_document_headers(), timeout=20.0)
                _cache_auth_token_from_response(resp)
                if resp.status_code < 400:
                    _ingest_players_from_html(resp.text or "", roster)
            except Exception as exc:
                logger.debug("ESPN squad HTML %s: %s", page_url, exc)
        time.sleep(0.12)

    _ROSTER_INDEX = dict(roster)
    _ROSTER_INDEX_BUILT = True
    logger.info("ESPN roster index: %s players from %s teams", len(_ROSTER_INDEX), len(teams))
    return len(_ROSTER_INDEX)


def _lookup_roster_index(display_name: str) -> Optional[Dict[str, str]]:
    if not _ROSTER_INDEX:
        return None
    key = _normalize_key(display_name)
    if key in _ROSTER_INDEX:
        return _ROSTER_INDEX[key]

    parts = key.split()
    if len(parts) >= 2:
        surname = parts[-1]
        first = parts[0]
        matches = []
        for k, v in _ROSTER_INDEX.items():
            kp = k.split()
            if not kp:
                continue
            if kp[-1] == surname and kp[0][:1] == first[:1]:
                matches.append(v)
        if len(matches) == 1:
            return matches[0]
    return None


def _search_legacy_ci(client: httpx.Client, display_name: str) -> Optional[Dict[str, Any]]:
    """Classic Statsguru-style search page — works without consumer auth."""
    html_pages: List[str] = []
    search_urls = [
        (LEGACY_PLAYER_SEARCH, {"search": display_name}),
        (
            "https://www.espncricinfo.com/ci/engine/player/index.html",
            {"search": display_name},
        ),
    ]
    for url, params in search_urls:
        try:
            resp = client.get(url, params=params, headers=_espn_document_headers(), timeout=20.0)
            if resp.status_code < 400 and resp.text:
                html_pages.append(resp.text)
        except Exception as exc:
            logger.debug("ESPN legacy search failed %s: %s", url, exc)

    if not html_pages:
        return None

    for html in html_pages:
        id_map = _map_player_ids_to_portraits(html)
        for m in re.finditer(
            r'href="(?:https://www\.espncricinfo\.com)?/cricketers/([a-z0-9-]+)-(\d+)"[^>]*>([^<]+)',
            html,
            re.IGNORECASE,
        ):
            slug, pid, label = m.group(1), m.group(2), m.group(3)
            label_name = _extract_ci_display_name(label)
            if _name_match(display_name, label_name) or _name_match(display_name, label):
                if pid in id_map:
                    return _portrait_row(slug, pid, id_map[pid], label_name or display_name)
                probe = _scrape_profile_page(client, slug, pid)
                if probe and _pick_image_url(probe):
                    return probe
                home = _consumer_home(client, pid)
                if home and _name_match(display_name, _player_name(home) or label_name):
                    return home
                if probe:
                    return probe

        links = _cricketer_links_from_html(html)
        for _path, slug, pid in links[:12]:
            probe = _scrape_profile_page(client, slug, pid)
            if not probe:
                home = _consumer_home(client, pid)
                if home and _name_match(display_name, _player_name(home) or ""):
                    return home
                continue
            pname = _player_name(probe) or ""
            if _name_match(display_name, pname):
                return probe
        if len(links) == 1:
            slug, pid = links[0][1], links[0][2]
            probe = _scrape_profile_page(client, slug, pid)
            if probe:
                return probe
            home = _consumer_home(client, pid)
            if home:
                return home
    return None


def espn_connectivity_self_check(client: httpx.Client) -> List[str]:
    """Return human-readable issues when ESPN cannot be reached or parsed."""
    issues: List[str] = []
    try:
        resp = client.get(
            LEGACY_PLAYER_SEARCH,
            params={"search": "Sam Curran"},
            headers=_espn_document_headers(),
            timeout=20.0,
        )
        if resp.status_code == 503:
            time.sleep(0.8)
            resp = client.get(
                LEGACY_PLAYER_SEARCH,
                params={"search": "Sam Curran"},
                headers=_espn_document_headers(),
                timeout=20.0,
            )
        if resp.status_code >= 400:
            issues.append(f"Legacy player search returned HTTP {resp.status_code}.")
            if resp.status_code in (401, 403) and not _espn_cookie_string():
                issues.append(
                    "Set ESPN_COOKIES from Chrome on espncricinfo.com "
                    "(Console: document.cookie), or ESPN_USE_PLAYWRIGHT=1."
                )
        elif "/cricketers/" not in (resp.text or ""):
            issues.append(
                "Legacy player search returned no /cricketers/ links (network block or bot wall)."
            )
    except Exception as exc:
        issues.append(f"Cannot reach ESPN Cricinfo ({exc}).")

    teams = _discover_ipl_team_ids(client)
    if len(teams) < 8:
        issues.append(f"Only resolved {len(teams)} IPL franchise pages (expected 10).")

    token = _consumer_auth_token(client)
    if not token:
        issues.append("ESPN consumer auth token not found (HTML/JSON fallback will be used).")
    return issues


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
        "playerImage",
        "headshot",
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


def _player_row_from_next_payload(payload: Any) -> Optional[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    _collect_player_dicts(payload, found)
    if not found:
        return None
    for row in found:
        if _pick_image_url(row) and _player_id(row):
            return row
    return found[0] if found else None


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
    return _player_row_from_next_payload(payload)


def _profile_url_from_parts(slug: str, player_id: str) -> str:
    slug_s = (slug or "player").strip().lower().replace(" ", "-")
    pid = str(player_id).strip()
    return f"{PROFILE_BASE}{slug_s}-{pid}"


def espn_uniform_url_map(
    conn: sqlite3.Connection, display_names: List[str],
) -> Dict[str, str]:
    """Map player_key -> ESPN uniform CDN URL (for players already seeded)."""
    from player_portrait_store import _normalize_key

    if not display_names:
        return {}
    ensure_espn_schema(conn)
    keys = {_normalize_key(n) for n in display_names if n and str(n).strip()}
    if not keys:
        return {}
    placeholders = ",".join("?" * len(keys))
    rows = conn.execute(
        f"""
        SELECT player_key, uniform_image_url
        FROM player_espn_cricinfo
        WHERE player_key IN ({placeholders})
          AND uniform_image_url IS NOT NULL AND TRIM(uniform_image_url) != ''
        """,
        tuple(keys),
    ).fetchall()
    out: Dict[str, str] = {}
    for key, url in rows:
        u = str(url or "").strip()
        if u.startswith("https://"):
            out[str(key)] = u
    return out


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
        {"searchText": query, "records": 20, "page": 1, "mode": "BOTH"},
        {"searchText": query, "records": 20, "page": 1},
        {"mode": "PREFIX", "searchText": query, "records": 20, "page": 1},
        {"mode": "prefix", "searchText": query, "records": 20, "page": 1},
        {"q": query, "limit": 20},
    ]
    headers = _consumer_request_headers(client)
    found: List[Dict[str, Any]] = []
    for params in param_sets:
        try:
            resp = client.get(
                CONSUMER_SEARCH,
                params=params,
                headers=headers,
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
            headers=_consumer_request_headers(client),
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
        r"href='(/cricketers/([a-z0-9-]+)-(\d+))'",
        r'href="(https://www\.espncricinfo\.com/cricketers/([a-z0-9-]+)-(\d+))"',
        r"href='(https://www\.espncricinfo\.com/cricketers/([a-z0-9-]+)-(\d+))'",
        r'"(?:url|href|path)"\s*:\s*"(?:https://www\.espncricinfo\.com)?/cricketers/([a-z0-9-]+)-(\d+)"',
        r"/cricketers/([a-z0-9-]+)-(\d+)",
    ]
    seen: set = set()
    out: List[Tuple[str, str, str]] = []
    for pat in patterns:
        for m in re.finditer(pat, html, re.IGNORECASE):
            g = m.groups()
            if len(g) == 3:
                path, slug, pid = g[0], g[1], g[2]
            elif len(g) == 2:
                slug, pid = g[0], g[1]
                path = f"/cricketers/{slug}-{pid}"
            else:
                continue
            key = (slug.lower(), pid)
            if key in seen:
                continue
            seen.add(key)
            out.append((path, slug, pid))
    return out


def _player_link_from_search_html(
    html: str, display_name: str,
) -> Optional[Tuple[str, str]]:
    """Best (slug, player_id) for display_name from search/legacy HTML."""
    for m in re.finditer(
        r'href=["\'](?:https://www\.espncricinfo\.com)?/cricketers/([a-z0-9-]+)-(\d+)["\'][^>]*>([^<]+)',
        html or "",
        re.IGNORECASE,
    ):
        slug, pid, label = m.group(1), m.group(2), m.group(3)
        label_name = _extract_ci_display_name(label)
        if _name_match(display_name, label_name) or _name_match(display_name, label):
            return slug, pid
    for _path, slug, pid in _cricketer_links_from_html(html):
        if _name_match(display_name, slug.replace("-", " ")):
            return slug, pid
    parsed = _parse_next_data_player(html)
    if parsed and _player_id(parsed) and _name_match(display_name, _player_name(parsed)):
        return _player_slug(parsed) or "player", _player_id(parsed) or ""
    return None


def _search_via_html(client: httpx.Client, display_name: str) -> Optional[Dict[str, Any]]:
    """Site search pages when consumer API is unavailable."""
    urls = [
        f"https://www.espncricinfo.com/search/results?q={quote(display_name)}",
        f"https://www.espncricinfo.com/ci/content/player/search.html?search={quote(display_name)}",
    ]
    links: List[Tuple[str, str, str]] = []
    for url in urls:
        try:
            resp = client.get(url, headers=_espn_document_headers(), timeout=18.0)
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


def _profile_row_from_html(
    slug: str, player_id: str, html: str, *, next_json: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    row = _parse_next_data_player(html) if html else None
    if not row and next_json:
        try:
            row = _player_row_from_next_payload(json.loads(next_json))
        except json.JSONDecodeError:
            pass
    if row:
        if not _player_id(row):
            row = {**row, "objectId": player_id}
        if not _player_slug(row):
            row = {**row, "slug": slug}
        if _pick_image_url(row):
            return row

    for blob in (html, next_json or ""):
        raw_url = _find_hscicdn_portrait_url(blob)
        if not raw_url and blob and blob.strip().startswith("{"):
            try:
                raw_url = _deep_hscicdn_in_obj(json.loads(blob))
            except json.JSONDecodeError:
                pass
        if raw_url:
            return {"objectId": player_id, "slug": slug, "imageUrl": raw_url}

    if html:
        og = re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            html,
            re.IGNORECASE,
        )
        if og and "hscicdn" in og.group(1):
            return {"objectId": player_id, "slug": slug, "imageUrl": og.group(1)}
    return None


def _playwright_launch(p: Any) -> Any:
    channel = os.getenv("ESPN_PLAYWRIGHT_CHANNEL", "").strip()
    launch_kw: Dict[str, Any] = {
        "headless": True,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if channel:
        launch_kw["channel"] = channel
    return p.chromium.launch(**launch_kw)


def _playwright_page_extract(page: Any, player_id: str) -> Tuple[Optional[str], str, Optional[str]]:
    """Return (image_url, html, next_json) from a loaded ESPN profile page."""
    data = page.evaluate(
        f"""async () => {{
            const out = {{ next: null, og: null, imgs: [], api: null, title: document.title || '' }};
            const el = document.getElementById('__NEXT_DATA__');
            if (el) out.next = el.textContent;
            const og = document.querySelector('meta[property="og:image"]');
            if (og) out.og = og.getAttribute('content') || '';
            const push = (s) => {{
                if (s && /hscicdn\\.com\\/image\\/upload/i.test(s)) out.imgs.push(s);
            }};
            document.querySelectorAll('img').forEach((img) => {{
                push(img.currentSrc || img.src || '');
                const ss = img.getAttribute('srcset') || '';
                ss.split(',').forEach((part) => push((part.trim().split(/\\s+/)[0] || '')));
            }});
            let auth = '';
            if (out.next) {{
                const m = out.next.match(/"x-hsci-auth-token"\\s*:\\s*"([^"]+)"/);
                if (m) auth = m[1];
            }}
            try {{
                const headers = {{
                    'Accept': 'application/json',
                    'Origin': 'https://www.espncricinfo.com',
                    'Referer': 'https://www.espncricinfo.com/',
                }};
                if (auth) headers['x-hsci-auth-token'] = auth;
                const r = await fetch(
                    '{CONSUMER_HOME}?playerId={player_id}&lang=en',
                    {{ credentials: 'include', headers }},
                );
                if (r.ok) out.api = await r.text();
            }} catch (e) {{}}
            return out;
        }}""",
    )
    html = page.content()
    next_json = (data or {}).get("next") if isinstance(data, dict) else None
    candidates: List[str] = []
    if isinstance(data, dict):
        for key in ("imgs",):
            for u in data.get(key) or []:
                if isinstance(u, str):
                    candidates.append(u)
        og = data.get("og")
        if isinstance(og, str) and og:
            candidates.append(og)
        api = data.get("api")
        if isinstance(api, str) and api:
            hit = _find_hscicdn_portrait_url(api)
            if not hit and api.strip().startswith("{"):
                try:
                    hit = _deep_hscicdn_in_obj(json.loads(api))
                except json.JSONDecodeError:
                    hit = None
            if hit:
                candidates.append(hit)
    for blob in (next_json or "", html):
        hit = _find_hscicdn_portrait_url(blob)
        if hit:
            candidates.append(hit)
        if blob and blob.strip().startswith("{"):
            try:
                deep = _deep_hscicdn_in_obj(json.loads(blob))
                if deep:
                    candidates.append(deep)
            except json.JSONDecodeError:
                pass
    for url in candidates:
        if _is_hscicdn_portrait_url(url):
            return url, html, next_json
    id_map = _map_player_ids_to_portraits(html)
    if player_id in id_map:
        return id_map[player_id], html, next_json
    if next_json:
        id_map = _map_player_ids_to_portraits(next_json)
        if player_id in id_map:
            return id_map[player_id], html, next_json
    return None, html, next_json


def _scrape_profile_playwright(slug: str, player_id: str) -> Optional[Dict[str, Any]]:
    """Headless Chromium — DOM images + in-page consumer API (uses session cookies)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning(
            "playwright not installed — pip install playwright && playwright install chromium",
        )
        return None

    url = _profile_url_from_parts(slug, player_id)
    debug = os.getenv("ESPN_DEBUG", "0").strip().lower() in ("1", "true", "yes")
    try:
        with sync_playwright() as p:
            browser = _playwright_launch(p)
            context = browser.new_context(
                user_agent=ESPN_HEADERS["User-Agent"],
                locale="en-US",
            )
            cookie_str = _espn_cookie_string()
            if cookie_str:
                pw_cookies = []
                for part in cookie_str.split(";"):
                    part = part.strip()
                    if not part or "=" not in part:
                        continue
                    key, _, val = part.partition("=")
                    pw_cookies.append({
                        "name": key.strip(),
                        "value": val.strip(),
                        "domain": ".espncricinfo.com",
                        "path": "/",
                    })
                if pw_cookies:
                    context.add_cookies(pw_cookies)
            page = context.new_page()
            page.goto("https://www.espncricinfo.com/", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1200)
            page.goto(url, wait_until="domcontentloaded", timeout=35000)
            try:
                page.wait_for_selector("#__NEXT_DATA__", timeout=15000)
            except Exception:
                pass
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                page.wait_for_timeout(3000)
            img_url, html, next_json = _playwright_page_extract(page, player_id)
            if not img_url and os.getenv("ESPN_DEBUG", "").strip().lower() in ("1", "true", "yes"):
                title = page.title()
                logger.warning(
                    "Playwright no portrait %s — title=%r hscicdn_in_html=%s",
                    url,
                    title,
                    "hscicdn" in (html or "").lower(),
                )
            browser.close()
    except Exception as exc:
        msg = f"ESPN Playwright profile failed {url}: {exc}"
        if debug:
            logger.warning(msg)
        else:
            logger.debug(msg)
        return None

    if next_json:
        token = _auth_token_from_html(
            f'<script id="__NEXT_DATA__">{next_json}</script>',
        )
        if token:
            global _AUTH_TOKEN_CACHE
            _AUTH_TOKEN_CACHE = (token, time.time())

    if img_url:
        return {"objectId": player_id, "slug": slug, "imageUrl": img_url}
    return _profile_row_from_html(slug, player_id, html, next_json=next_json)


def _scrape_profile_page(
    client: httpx.Client, slug: str, player_id: str,
) -> Optional[Dict[str, Any]]:
    url = _profile_url_from_parts(slug, player_id)
    html: Optional[str] = None
    status = 0
    for attempt in range(2):
        try:
            resp = client.get(
                url,
                headers=_espn_document_headers(referer="https://www.espncricinfo.com/"),
                timeout=20.0,
            )
            _cache_auth_token_from_response(resp)
            status = resp.status_code
            if status < 400:
                html = resp.text
                break
            if status == 503 and attempt == 0:
                time.sleep(1.0)
        except Exception as exc:
            logger.debug("ESPN profile fetch failed %s: %s", url, exc)
            break

    portrait_url = ""
    if html:
        row = _profile_row_from_html(slug, player_id, html)
        if row:
            portrait_url = _pick_image_url(row) or row.get("imageUrl") or ""
        if not portrait_url:
            portrait_url = _find_hscicdn_portrait_url(html) or ""
        if portrait_url:
            return {"objectId": player_id, "slug": slug, "imageUrl": portrait_url}

    if _use_playwright() or _playwright_auto() or not html or status >= 400:
        pw = _scrape_profile_playwright(slug, player_id)
        if pw and (_pick_image_url(pw) or pw.get("imageUrl")):
            return pw
    return None


def _ensure_roster_index(client: httpx.Client) -> None:
    if _ROSTER_INDEX:
        return
    if os.getenv("ESPN_AUTO_WARM_ROSTER", "1").strip().lower() not in ("1", "true", "yes"):
        return
    try:
        warm_espn_roster_index(client)
    except Exception as exc:
        logger.debug("ESPN auto roster warm failed: %s", exc)


def resolve_espn_player(
    client: httpx.Client,
    display_name: str,
    *,
    espn_player_id: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[Dict[str, str]]:
    """
    Resolve ESPN player id + uniform portrait URL for a display name.
    Returns dict with keys: espn_player_id, espn_slug, raw_image_url, uniform_image_url.
    """
    clean = display_name.strip()
    if not clean:
        return None

    _ensure_roster_index(client)

    if not espn_player_id and conn is not None:
        espn_player_id = _lookup_auction_espn_id(conn, clean)

    row: Optional[Dict[str, Any]] = None
    if espn_player_id:
        row = _consumer_home(client, espn_player_id)
        if not row or not _pick_image_url(row):
            row = _scrape_profile_page(client, "player", espn_player_id)

    roster_hit = _lookup_roster_index(clean)
    if not row and roster_hit:
        raw = roster_hit.get("raw_image_url") or ""
        slug = roster_hit["espn_slug"]
        pid = roster_hit["espn_player_id"]
        if not raw:
            search_blob = _legacy_search_html(client, clean)
            pmap = _map_player_ids_to_portraits(search_blob)
            if pid in pmap:
                raw = pmap[pid]
        if not raw:
            scraped = _scrape_profile_page(client, slug, pid)
            if scraped:
                row = scraped
                raw = _pick_image_url(scraped) or scraped.get("imageUrl") or ""
        if not raw:
            pw = _scrape_profile_playwright(slug, pid)
            if pw:
                row = pw
                raw = _pick_image_url(pw) or pw.get("imageUrl") or ""
        if not raw:
            home = _consumer_home(client, pid)
            if home:
                raw = _pick_image_url(home) or _deep_hscicdn_in_obj(home) or ""
                if raw:
                    row = home
        if not row:
            row = {
                "objectId": pid,
                "slug": slug,
                "longName": roster_hit["display_name"],
                "imageUrl": raw,
            }

    if not row:
        row = _search_legacy_ci(client, clean)

    if not row:
        hits = _consumer_search(client, clean)
        row = _best_search_hit(clean, hits)

    if not row:
        row = _search_via_html(client, clean)

    if not row:
        return None

    resolved = _row_to_resolved(row)
    if resolved:
        return resolved

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
        resolved = resolve_espn_player(client, clean, espn_player_id=espn_id, conn=conn)
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
