"""
IPL official face-card URLs (documents.iplt20.com) — URL only, no image bytes.

Index built from iplt20.com team squad pages; stored per auction player_key.

Note: iplt20.com/players/ms-dhoni/1 uses route id 1 — NOT the IPLHeadshot CDN id.
Headshot ids come only from squad pages (e.g. IPLHeadshot2026/102.png).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from player_portrait_store import _connect, _normalize_key

logger = logging.getLogger(__name__)

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

HEADSHOT_YEARS = [2026, 2025, 2024, 2023, 2022]

IPL_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.iplt20.com/",
}

_SKIP_NAME_FRAGMENTS = frozenset({
    "view profile", "view more", "full squad", "squad", "chennai", "super", "kings",
    "mumbai", "indians", "royal", "challengers", "kolkata", "knight", "riders",
    "delhi", "capitals", "punjab", "rajasthan", "sunrisers", "hyderabad",
    "gujarat", "titans", "lucknow", "giants", "bengaluru", "rest of the squad",
})

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


def _looks_like_player_name(name: str) -> bool:
    text = (name or "").strip()
    if len(text) < 3 or len(text) > 45:
        return False
    low = text.lower()
    if low in _SKIP_NAME_FRAGMENTS:
        return False
    if not re.match(r"^[A-Za-z][A-Za-z.' -]+$", text):
        return False
    parts = text.split()
    if len(parts) < 2:
        return False
    return True


def _slug_to_display_name(slug: str) -> str:
    """ms-dhoni -> MS Dhoni, sam-curran -> Sam Curran."""
    text = (slug or "").strip().lower().replace("-", " ")
    parts = [p for p in text.split() if p]
    if not parts:
        return ""
    out: List[str] = []
    for i, p in enumerate(parts):
        if i == 0 and len(p) <= 3:
            out.append(p.upper())
        else:
            out.append(p.capitalize())
    return " ".join(out)


def _name_from_context(blob: str) -> Optional[str]:
    """Extract player display name near a headshot reference in squad HTML."""
    if not blob:
        return None
    patterns = (
        r'alt="([A-Za-z][A-Za-z.\' -]{2,45})"',
        r'title="([A-Za-z][A-Za-z.\' -]{2,45})"',
        r'aria-label="([A-Za-z][A-Za-z.\' -]{2,45})"',
        r'"fullName"\s*:\s*"([^"]+)"',
        r'"playerName"\s*:\s*"([^"]+)"',
        r'"name"\s*:\s*"([A-Z][a-zA-Z.\' -]{2,45})"',
        r">([A-Z][a-z]+(?:\s+[A-Z][a-z.'-]+){1,3})<",
        r">\s*([A-Z]{1,3}\s+[A-Z][a-z]+(?:\s+[A-Z][a-z.'-]+)?)\s*<",
        r'player-name[^>]*>([A-Za-z][A-Za-z.\' -]{2,45})<',
    )
    for pat in patterns:
        for m in re.finditer(pat, blob):
            name = m.group(1).strip()
            if _looks_like_player_name(name):
                return name
    route = re.search(r"/players/([a-z0-9-]+)/(\d+)", blob, re.I)
    if route:
        from_slug = _slug_to_display_name(route.group(1))
        if _looks_like_player_name(from_slug):
            return from_slug
    return None


def _year_from_headshot_token(token: str) -> Optional[int]:
    m = re.search(r"IPLHeadshot(\d{4})", token or "", re.I)
    return int(m.group(1)) if m else None


def facecard_url_candidates(player_id: str) -> List[str]:
    """CDN URLs to try (GET, not HEAD — S3 often 404 on HEAD)."""
    pid = str(player_id).strip()
    if not pid:
        return []
    out: List[str] = []
    seen: set = set()
    for year in HEADSHOT_YEARS:
        for ext in ("png", "webp", "jpg"):
            url = f"https://documents.iplt20.com/ipl/IPLHeadshot{year}/{pid}.{ext}"
            if url not in seen:
                seen.add(url)
                out.append(url)
    return out


def _response_is_image(resp: httpx.Response) -> bool:
    if resp.status_code != 200 or len(resp.content or b"") < 400:
        return False
    ct = (resp.headers.get("content-type") or "").lower()
    if ct.startswith("image/"):
        return True
    head = (resp.content or b"")[:12]
    return head.startswith(b"\x89PNG") or head[:3] == b"\xff\xd8\xff" or head[:4] == b"RIFF"


def find_working_facecard_url(
    client: httpx.Client, player_id: str, *, prefer_url: Optional[str] = None,
) -> Tuple[Optional[str], Optional[int]]:
    """Return (url, year) for the first URL that returns a real image (GET)."""
    urls: List[str] = []
    if prefer_url and prefer_url.strip().startswith("https://"):
        urls.append(prefer_url.strip())
    urls.extend(facecard_url_candidates(player_id))
    seen: set = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        try:
            resp = client.get(url, headers=IPL_UA, timeout=12.0, follow_redirects=True)
            if _response_is_image(resp):
                year = _year_from_headshot_token(url)
                return url, year
        except Exception as exc:
            logger.debug("IPL facecard probe failed %s: %s", url, exc)
    return None, None


def debug_nearest_headshot_for_slug(html: str, player_slug: str) -> Optional[Dict[str, str]]:
    """Debug helper: nearest IPLHeadshot id to /players/{slug}/ in squad HTML."""
    slug_l = (player_slug or "").strip().lower()
    if not slug_l or not html:
        return None
    routes = [
        (m.start(), m.group(1))
        for m in re.finditer(r"/players/([a-z0-9-]+)/(\d+)", html, re.I)
        if m.group(1).lower() == slug_l
    ]
    headshots = [
        (m.start(), m.group(1), m.group(2), m.group(3))
        for m in re.finditer(r"IPLHeadshot(\d{4})/(\d+)\.(png|webp|jpg)", html, re.I)
    ]
    if not routes or not headshots:
        return None
    r_pos, slug = routes[0]
    best_dist = 10**9
    best: Optional[Tuple[str, str, str]] = None
    for h_pos, year, pid, ext in headshots:
        dist = abs(h_pos - r_pos)
        if dist < best_dist:
            best_dist = dist
            best = (year, pid, ext)
    if not best:
        return None
    year, pid, ext = best
    return {
        "slug": slug,
        "display_name": _slug_to_display_name(slug),
        "espn_route_pos": r_pos,
        "distance_chars": best_dist,
        "facecard_url": f"https://documents.iplt20.com/ipl/IPLHeadshot{year}/{pid}.{ext}",
        "ipl_player_id": pid,
    }


def _ingest_route_headshot_pairs(
    html: str,
    team_slug: str,
    index: Dict[str, Dict[str, str]],
    _add,
    *,
    max_distance: int = 8000,
) -> None:
    """
    Pair each /players/{slug}/ link with the geographically nearest IPLHeadshot token.
    Fixes CSK layout where names sit above images and id 102 is Ruturaj, not Dhoni.
    """
    routes = [
        (m.start(), m.group(1), m.group(2))
        for m in re.finditer(r"/players/([a-z0-9-]+)/(\d+)", html, re.I)
    ]
    headshots = [
        (m.start(), m.group(1), m.group(2), m.group(3))
        for m in re.finditer(r"IPLHeadshot(\d{4})/(\d+)\.(png|webp|jpg)", html, re.I)
    ]
    if not routes or not headshots:
        return

    for r_pos, slug, _route_id in routes:
        best_dist = max_distance + 1
        nearest: Optional[Tuple[str, str, str]] = None
        for h_pos, year, pid, ext in headshots:
            dist = abs(h_pos - r_pos)
            if dist < best_dist:
                best_dist = dist
                nearest = (year, pid, ext)
        if not nearest or best_dist > max_distance:
            continue
        year, pid, ext = nearest
        url = f"https://documents.iplt20.com/ipl/IPLHeadshot{year}/{pid}.{ext}"
        name = _slug_to_display_name(slug)
        _add(pid, name, url, source="route")


def _ingest_squad_html(html: str, team_slug: str, index: Dict[str, Dict[str, str]]) -> None:
    """Parse squad HTML into index entries {id, display_name, team, facecard_url?}."""

    def _add(
        pid: str,
        name: Optional[str],
        facecard_url: Optional[str] = None,
        *,
        source: str = "",
    ) -> None:
        if not pid or not name or not _looks_like_player_name(name):
            return
        key = normalize_ipl_name(name)
        if not key:
            return
        entry = {
            "id": str(pid),
            "display_name": name.strip(),
            "team": team_slug,
        }
        if facecard_url:
            entry["facecard_url"] = facecard_url.split("?")[0]
        if key in index:
            # Route/slug pairing wins; do not let chunk heuristics overwrite names
            if source == "route" or (facecard_url and not index[key].get("facecard_url")):
                index[key].update(entry)
            return
        index[key] = entry

    # 1) Route slug <-> nearest headshot (most reliable for MS Dhoni, Sam Curran, etc.)
    _ingest_route_headshot_pairs(html, team_slug, index, _add)

    # 2) Split page into headshot-centred chunks (names often appear BEFORE the image token)
    parts = re.split(r"(IPLHeadshot\d{4}/\d+\.(?:png|webp|jpg))", html, flags=re.I)
    for i in range(1, len(parts), 2):
        token = parts[i]
        hm = re.match(r"IPLHeadshot(\d{4})/(\d+)\.(png|webp|jpg)", token, re.I)
        if not hm:
            continue
        year, pid, ext = hm.group(1), hm.group(2), hm.group(3)
        url = f"https://documents.iplt20.com/ipl/IPLHeadshot{year}/{pid}.{ext}"
        before = parts[i - 1] if i > 0 else ""
        after = parts[i + 1] if i + 1 < len(parts) else ""
        blob = (before[-2500:] if before else "") + token + (after[:2500] if after else "")
        name = _name_from_context(blob)
        if normalize_ipl_name(name or "") not in index:
            _add(pid, name, url, source="chunk")

    # 3) Full documents.iplt20.com URLs embedded in page
    for m in re.finditer(
        r"https://documents\.iplt20\.com/ipl/(IPLHeadshot\d{4})/(\d+)\.(png|webp|jpg)",
        html,
        re.IGNORECASE,
    ):
        url = m.group(0).split("?")[0]
        pid = m.group(2)
        window = html[max(0, m.start() - 1500): m.end() + 1500]
        name = _name_from_context(window)
        if normalize_ipl_name(name or "") not in index:
            _add(pid, name, url, source="url")

    # 4) __NEXT_DATA__ / inline JSON scripts
    nd = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.I,
    )
    if nd:
        try:
            payload = json.loads(nd.group(1))
            _walk_next_players(payload, team_slug, index)
        except json.JSONDecodeError:
            pass
    for sm in re.finditer(
        r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.I,
    ):
        try:
            payload = json.loads(sm.group(1))
            _walk_next_players(payload, team_slug, index)
        except json.JSONDecodeError:
            continue


def _walk_next_players(obj: Any, team_slug: str, index: Dict[str, Dict[str, str]]) -> None:
    """Find dicts that look like squad player records in __NEXT_DATA__."""
    stack: List[Any] = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            name = (
                cur.get("fullName")
                or cur.get("name")
                or cur.get("playerName")
                or cur.get("title")
            )
            pid = (
                cur.get("iplPlayerId")
                or cur.get("playerId")
                or cur.get("id")
            )
            img = (
                cur.get("headshot")
                or cur.get("imageUrl")
                or cur.get("playerImage")
                or cur.get("profileImage")
            )
            if isinstance(name, str) and pid is not None and _looks_like_player_name(name):
                pid_s = str(pid).strip()
                url = ""
                if isinstance(img, str) and "documents.iplt20.com" in img:
                    url = img
                elif isinstance(img, dict):
                    url = str(img.get("url") or img.get("src") or "")
                key = normalize_ipl_name(name)
                if key and key not in index:
                    entry: Dict[str, str] = {
                        "id": pid_s,
                        "display_name": name.strip(),
                        "team": team_slug,
                    }
                    if url.startswith("https://"):
                        entry["facecard_url"] = url.split("?")[0]
                    index[key] = entry
            for v in cur.values():
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)


def build_squad_index(client: httpx.Client) -> Dict[str, Dict[str, str]]:
    """Scrape all team squad pages -> {normalized_name: {id, display_name, team, facecard_url?}}."""
    index: Dict[str, Dict[str, str]] = {}
    for slug in ROOT_TEAM_SLUGS:
        url = f"https://www.iplt20.com/teams/{slug}/squad"
        try:
            resp = client.get(url, headers=IPL_UA, timeout=25.0)
            if resp.status_code >= 400:
                logger.debug("IPL squad page HTTP %s %s", resp.status_code, url)
                continue
            _ingest_squad_html(resp.text or "", slug, index)
        except Exception as exc:
            logger.debug("IPL squad scrape failed %s: %s", url, exc)
        time.sleep(0.25)
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

    # Surname-only fallback when unique (e.g. auction "Sam Curran" vs squad index timing)
    if len(parts) >= 2:
        surname = parts[-1]
        by_surname = [v for k, v in index.items() if k.split() and k.split()[-1] == surname]
        if len(by_surname) == 1:
            return by_surname[0]
    return None


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


def has_ipl_facecard(conn: sqlite3.Connection, display_name: str) -> bool:
    """True when a non-empty IPL face-card URL is stored for this player."""
    row = lookup_facecard(conn, display_name)
    if not row:
        return False
    url = str(row.get("facecard_url") or "").strip()
    return url.startswith("https://")


def fetch_facecard_image_bytes(display_name: str) -> Optional[Tuple[bytes, str]]:
    """
    Download IPL headshot bytes for arena / avatar proxy (same-origin to browser).
    Returns (image_data, content_type) or None.
    """
    clean = (display_name or "").strip()
    if not clean:
        return None
    conn = _connect()
    try:
        row = lookup_facecard(conn, clean)
        if not row:
            return None
        url = str(row.get("facecard_url") or "").strip()
        if not url.startswith("https://"):
            return None
        headers = {
            **IPL_UA,
            "Accept": "image/avif,image/webp,image/apng,image/png,image/jpeg,*/*;q=0.8",
        }
        with httpx.Client(follow_redirects=True, trust_env=False, timeout=12.0) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code != 200:
                return None
            data = resp.content
            if len(data) < 400:
                return None
            ct = (resp.headers.get("content-type") or "image/png").split(";")[0].strip()
            if not ct.startswith("image/"):
                return None
            return data, ct
    except Exception as exc:
        logger.debug("Facecard fetch failed for %s: %s", clean, exc)
        return None
    finally:
        conn.close()
