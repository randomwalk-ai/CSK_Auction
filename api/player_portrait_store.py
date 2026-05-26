"""
Player portraits — multi-source resolver with SQLite cache in auction_data.db.

Flow:
  1. Verify cricketer profile (Groq when GROQ_API_KEY set + auction pool) → player_identity_cache
  2. If verified → fetch portrait (ESPN Cricinfo → Cricbuzz → TheSportsDB → Wikipedia)
  3. Cache image in player_portraits + data/player_portraits/
  4. Else → initials SVG only

Priority (first hit wins):
  1. ESPN Cricinfo (uniform square headshots via hscicdn)
  2. Cricbuzz CDN (player ID from auction_prices_full)
  3. TheSportsDB strThumb (cricket + name match)
  4. Wikipedia / Wikimedia (verified cricketer page)
  5. Initials SVG fallback

Name resolution (v3 — zero hardcoding):
  - Canonical names fetched live from Cricbuzz profile API using cricbuzz_player_id.
  - PDF spacing artifacts detected algorithmically (no name list).
  - Equivalent-key groups built from shared cricbuzz_player_id in the DB.
  - All derived mappings cached in SQLite (player_name_aliases table).

Wikipedia (v2):
  - Validates REST description/extract for cricket keywords before accepting.
  - trust_title skips name check only, never sport verification.
  - Search uses "X cricketer" queries only; each hit verified via summary fetch.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import hashlib
import time
import unicodedata
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import quote

import httpx

from cricketer_identity import ensure_identity_schema, resolve_cricketer_profile

try:
    from env_loader import load_project_dotenv
    load_project_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = os.getenv("DB_PATH", str(ROOT / "auction_data.db"))
PORTRAIT_DIR = Path(os.getenv("PORTRAIT_DIR", str(ROOT / "data" / "player_portraits")))
MANIFEST_PATH = PORTRAIT_DIR / "manifest.json"
THESPORTSDB_KEY = os.getenv("THESPORTSDB_KEY", "3")
THESPORTSDB_BASE = f"https://www.thesportsdb.com/api/v1/json/{THESPORTSDB_KEY}"
WIKI_API = "https://en.wikipedia.org/w/api.php"

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HTTP_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/json, image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}
WIKI_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/json",
}
CRICBUZZ_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Referer": "https://www.cricbuzz.com/",
    "Origin": "https://www.cricbuzz.com",
    "Accept-Language": "en-US,en;q=0.9",
}

PHOTO_SOURCES = frozenset({"espncricinfo", "cricbuzz", "thesportsdb", "wikipedia"})


def _prefer_espn_portraits() -> bool:
    """When true (default), non-ESPN cached photos are refreshed so ESPN can replace them."""
    return os.getenv("PREFER_ESPN_PORTRAITS", "1").strip().lower() not in ("0", "false", "no")


def _skip_espn_portraits() -> bool:
    """When true, portrait fetch skips ESPN (use Cricbuzz-first seeding)."""
    return os.getenv("PORTRAIT_SKIP_ESPN", "0").strip().lower() in ("1", "true", "yes")


def _use_cached_photo(cached: Optional[Tuple[bytes, str, str]]) -> bool:
    if not cached or cached[2] not in PHOTO_SOURCES:
        return False
    if cached[2] == "espncricinfo":
        return True
    return not _prefer_espn_portraits()

# ---------------------------------------------------------------------------
# Wikipedia cricket verification
# ---------------------------------------------------------------------------

_CRICKET_KEYWORDS = frozenset({
    "cricketer", "cricket", "batsman", "batter", "bowler", "wicket-keeper",
    "wicketkeeper", "all-rounder", "test cricket", "ipl", "indian premier league",
    "t20", "one-day international", "odi", "bcci", "first-class cricket",
    "county cricket", "ranji", "cricket player", "international cricket",
})

_NON_CRICKET_ROLES = frozenset({
    "politician", "actor", "actress", "singer", "musician", "rapper",
    "footballer", "football player", "soccer player", "basketball player",
    "tennis player", "boxer", "wrestler",
    "entrepreneur", "businessman", "businesswoman", "investor",
    "lawyer", "judge", "barrister",
    "film director", "producer", "television presenter",
    "author", "writer", "novelist", "poet", "journalist",
    "painter", "artist", "sculptor",
    "doctor", "physician", "scientist", "professor",
})

def _page_is_cricketer(summary_data: dict, display_name: str) -> bool:
    """True only if Wikipedia summary page is clearly about a cricketer."""
    description = (summary_data.get("description") or "").lower()
    extract = (summary_data.get("extract") or "")[:400].lower()
    title = (summary_data.get("title") or "").lower()
    text_blob = f"{description} {extract} {title}"

    for role in _NON_CRICKET_ROLES:
        if role in description:
            logger.debug("Wikipedia page for '%s' rejected — description says '%s'",
                         display_name, description)
            return False

    for kw in _CRICKET_KEYWORDS:
        if kw in text_blob:
            return True

    logger.debug("Wikipedia page for '%s' has no cricket keywords (desc='%s')",
                 display_name, description)
    return False

# ---------------------------------------------------------------------------
# Core name utilities (no hardcoded names)
# ---------------------------------------------------------------------------

def _normalize_key(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())

def _compact_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _normalize_key(_strip_accents(name)))

def _strip_accents(text: str) -> str:
    nf = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in nf if not unicodedata.combining(ch))

def _initials(name: str) -> str:
    parts = [p for p in re.split(r"\s+", name.strip()) if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[-1][0]).upper()
    return (parts[0][:2] if parts else "?").upper()

def initials_svg(name: str) -> bytes:
    ini = _initials(name)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">'
        f'<circle cx="64" cy="64" r="64" fill="#004a99"/>'
        f'<text x="64" y="78" text-anchor="middle" fill="#ffcc00" font-size="40" '
        f'font-weight="700" font-family="Arial,Helvetica,sans-serif">{ini}</text>'
        f'</svg>'
    )
    return svg.encode("utf-8")

# ---------------------------------------------------------------------------
# PDF spacing-artifact repair  (algorithmic — no name list)
#
# Heuristic: a word looks like a PDF split artifact when it is 1–3 chars long
# AND its compact form is a prefix of the *next* word's compact form, OR when
# consecutive short tokens reconstruct a plausible single word seen in
# the DB's canonical name index.
# ---------------------------------------------------------------------------

def _spaced_name_for_compact(conn: sqlite3.Connection, compact: str) -> Optional[str]:
    """Best DB player_name whose compact key matches (prefers spaced spellings)."""
    target = compact.lower()
    best: Optional[str] = None
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT player_name FROM auction_prices_full
            WHERE year = 2026
              AND player_name IS NOT NULL
              AND TRIM(player_name) != ''
            """
        ).fetchall()
    except sqlite3.Error:
        return None
    for (name,) in rows:
        n = str(name).strip()
        if _compact_key(n) != target:
            continue
        if best is None or (" " in n and " " not in best) or len(n) > len(best or ""):
            best = n
    return best


def _format_repaired_tokens(
    tokens: List[str], conn: Optional[sqlite3.Connection] = None,
) -> str:
    parts: List[str] = []
    for tok in tokens:
        if conn and " " not in tok:
            spaced = _spaced_name_for_compact(conn, _compact_key(tok))
            if spaced:
                parts.append(spaced)
                continue
        parts.append(tok.capitalize() if tok.islower() else tok)
    return " ".join(parts)


def _repair_pdf_name(
    raw: str,
    canonical_index: Optional[Set[str]] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> str:
    """
    Attempt to repair PDF-split names like "ja mie overton" → "Jamie Overton".

    Strategy (no hardcoded names):
      1. Split into tokens.
      2. Greedily merge adjacent tokens when merging produces a token whose
         compact form matches a token in the canonical_index (names already
         known to be correct from the DB / Cricbuzz).
      3. Fallback: merge any run of tokens that are individually ≤3 chars
         and together form a word ≥5 chars (catches "ja mie" → "jamie").
    """
    tokens = raw.strip().split()
    if len(tokens) <= 1:
        return raw.strip()

    # Pass 1 — merge against canonical index if available
    if canonical_index:
        merged = _merge_against_index(tokens, canonical_index)
        if merged != tokens:
            result = _format_repaired_tokens(merged, conn)
            logger.debug("PDF repair (index): '%s' → '%s'", raw, result)
            return result

    # Pass 2 — merge purely short consecutive tokens
    merged = _merge_short_tokens(tokens)
    if merged != tokens:
        result = _format_repaired_tokens(merged, conn)
        logger.debug("PDF repair (short-token): '%s' → '%s'", raw, result)
        return result

    return raw.strip()

def _merge_against_index(tokens: List[str], index: Set[str]) -> List[str]:
    """Merge adjacent tokens whose concatenation appears in index.

    Only considers runs that include at least one short (≤3 char) token so
    valid two-word names like "Ruturaj Gaikwad" are not collapsed.
    """
    result: List[str] = []
    i = 0
    while i < len(tokens):
        merged = False
        for j in range(len(tokens), i, -1):
            if j - i <= 1:
                break
            chunk = tokens[i:j]
            short_count = sum(1 for t in chunk if len(t) <= 3)
            if short_count < 2 and not all(len(t) <= 3 for t in chunk):
                continue
            candidate = "".join(chunk).lower()
            if candidate in index:
                result.append(candidate)
                i = j
                merged = True
                break
        if not merged:
            result.append(tokens[i])
            i += 1
    return result

def _merge_short_tokens(tokens: List[str]) -> List[str]:
    """Merge runs of tokens that are each ≤3 chars into single words."""
    result: List[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if len(tok) <= 3 and i + 1 < len(tokens) and len(tokens[i + 1]) <= 3:
            # Start a merge run
            run = [tok]
            j = i + 1
            while j < len(tokens) and len(tokens[j]) <= 3:
                run.append(tokens[j])
                j += 1
            merged = "".join(run)
            # Only merge if the result looks like a plausible word (≥4 chars,
            # not all consonants)
            vowels = set("aeiou")
            if len(merged) >= 4 and any(c in vowels for c in merged):
                result.append(merged)
                i = j
                continue
        result.append(tok)
        i += 1
    return result

# ---------------------------------------------------------------------------
# Alias / canonical name cache  (SQLite-backed, built from Cricbuzz API)
# ---------------------------------------------------------------------------

_ALIAS_SCHEMA = """
CREATE TABLE IF NOT EXISTS player_name_aliases (
    raw_key        TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    cricbuzz_id    TEXT,
    source         TEXT NOT NULL,
    updated_at     TEXT NOT NULL
)
"""

_EQUIV_SCHEMA = """
CREATE TABLE IF NOT EXISTS player_name_equivalents (
    key_a      TEXT NOT NULL,
    key_b      TEXT NOT NULL,
    cricbuzz_id TEXT NOT NULL,
    PRIMARY KEY (key_a, key_b)
)
"""

def _ensure_alias_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_ALIAS_SCHEMA)
    conn.execute(_EQUIV_SCHEMA)
    conn.commit()

def _read_alias_cache(conn: sqlite3.Connection, raw_key: str) -> Optional[str]:
    _ensure_alias_schema(conn)
    row = conn.execute(
        "SELECT canonical_name FROM player_name_aliases WHERE raw_key = ?",
        (raw_key,),
    ).fetchone()
    return str(row[0]) if row else None

def _write_alias_cache(
    conn: sqlite3.Connection,
    raw_key: str,
    canonical_name: str,
    cricbuzz_id: Optional[str],
    source: str,
) -> None:
    _ensure_alias_schema(conn)
    conn.execute(
        """
        INSERT OR REPLACE INTO player_name_aliases
        (raw_key, canonical_name, cricbuzz_id, source, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            raw_key, canonical_name, cricbuzz_id, source,
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.commit()

def _load_equivalent_keys(conn: sqlite3.Connection) -> Set[frozenset]:
    """Load all equivalent-name groups from DB (built from shared cricbuzz_id)."""
    _ensure_alias_schema(conn)
    rows = conn.execute(
        "SELECT key_a, key_b FROM player_name_equivalents"
    ).fetchall()
    return {frozenset({str(r[0]), str(r[1])}) for r in rows}

def _rebuild_equivalent_keys(conn: sqlite3.Connection) -> None:
    """
    Detect names that share a cricbuzz_player_id in auction_prices_full —
    these are the same player under different spellings. Store as equivalents.
    No hardcoded name lists.
    """
    _ensure_alias_schema(conn)
    try:
        rows = conn.execute(
            """
            SELECT cricbuzz_player_id, GROUP_CONCAT(player_name, '||') AS names
            FROM auction_prices_full
            WHERE year = 2026
              AND cricbuzz_player_id IS NOT NULL
              AND TRIM(cricbuzz_player_id) != ''
              AND player_name IS NOT NULL
              AND TRIM(player_name) != ''
            GROUP BY cricbuzz_player_id
            HAVING COUNT(DISTINCT player_name) > 1
            """
        ).fetchall()
    except sqlite3.Error as exc:
        logger.debug("Could not build equivalent keys: %s", exc)
        return

    conn.execute("DELETE FROM player_name_equivalents")
    for cid, names_concat in rows:
        names = [n.strip() for n in str(names_concat).split("||") if n.strip()]
        keys = [_normalize_key(n) for n in names]
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                conn.execute(
                    "INSERT OR IGNORE INTO player_name_equivalents (key_a, key_b, cricbuzz_id) VALUES (?, ?, ?)",
                    (keys[i], keys[j], str(cid)),
                )
    conn.commit()
    logger.info("Rebuilt player_name_equivalents from shared cricbuzz_player_id")

def _equivalent_player_keys(a: str, b: str, conn: Optional[sqlite3.Connection] = None) -> bool:
    """True if a and b are known spellings of the same player."""
    if _normalize_key(a) == _normalize_key(b):
        return True
    if conn is None:
        return False
    equiv_set = _load_equivalent_keys(conn)
    pair = frozenset({_normalize_key(a), _normalize_key(b)})
    return pair in equiv_set

# ---------------------------------------------------------------------------
# Cricbuzz canonical name resolution
# ---------------------------------------------------------------------------

def _fetch_cricbuzz_canonical_name(
    client: httpx.Client, cricbuzz_id: str
) -> Optional[str]:
    """
    Fetch the player's canonical full name from the Cricbuzz player profile API.
    Returns None on failure. No hardcoded name mapping.
    """
    pid = str(cricbuzz_id).strip()
    # Cricbuzz player profile JSON endpoint
    url = f"https://www.cricbuzz.com/api/cricket-players/{pid}"
    try:
        resp = client.get(url, headers=CRICBUZZ_HEADERS, timeout=8.0)
        if resp.status_code == 200:
            data = resp.json()
            # Field is "fullName" in Cricbuzz profile API
            name = (
                data.get("fullName")
                or data.get("name")
                or data.get("playerName")
            )
            if name and str(name).strip():
                return str(name).strip()
    except Exception as exc:
        logger.debug("Cricbuzz profile fetch failed for id=%s: %s", pid, exc)

    # Fallback: scrape the player page title
    page_url = f"https://www.cricbuzz.com/profiles/{pid}"
    try:
        resp = client.get(page_url, headers={**CRICBUZZ_HEADERS, "Accept": "text/html"}, timeout=8.0)
        if resp.status_code == 200:
            # <title>PlayerName | ...</title>
            m = re.search(r"<title>\s*([^|<]+?)\s*\|", resp.text)
            if m:
                return m.group(1).strip()
    except Exception as exc:
        logger.debug("Cricbuzz page scrape failed for id=%s: %s", pid, exc)

    return None

def _resolve_display_name(
    name: str,
    conn: Optional[sqlite3.Connection] = None,
    client: Optional[httpx.Client] = None,
) -> str:
    """
    Resolve a raw player name to its canonical form.

    Order:
      1. SQLite alias cache (instant)
      2. PDF artifact repair (algorithmic)
      3. Cricbuzz profile API (if cricbuzz_id known and client provided)
      4. Return name as-is
    """
    raw_key = _normalize_key(name)

    # 1. Alias cache
    if conn is not None:
        cached = _read_alias_cache(conn, raw_key)
        if cached:
            return cached

    # 2. PDF artifact repair
    canonical_index: Optional[Set[str]] = None
    if conn is not None:
        canonical_index = _load_canonical_index(conn)
    repaired = _repair_pdf_name(name, canonical_index, conn=conn)
    repaired_key = _normalize_key(repaired)

    # If repair changed the name, cache and return
    if repaired_key != raw_key:
        if conn is not None:
            _write_alias_cache(conn, raw_key, repaired, None, "pdf_repair")
        return repaired

    # 3. Cricbuzz canonical name (if we have a cricbuzz_id and a live client)
    if conn is not None and client is not None:
        cric_id = _lookup_cricbuzz_id(conn, name)
        if cric_id:
            canonical = _fetch_cricbuzz_canonical_name(client, cric_id)
            if canonical:
                _write_alias_cache(conn, raw_key, canonical, cric_id, "cricbuzz_api")
                return canonical

    return name.strip()

@lru_cache(maxsize=1)
def _load_canonical_index_cached() -> Set[str]:
    """
    Load compact keys of all known-good player names from the DB.
    Used by the PDF repair heuristic. Cached for the process lifetime;
    call _load_canonical_index_cached.cache_clear() after a DB rebuild.
    """
    conn = _connect_raw()
    try:
        return _load_canonical_index(conn)
    finally:
        conn.close()

def _load_canonical_index(conn: sqlite3.Connection) -> Set[str]:
    """
    Return the set of compact (no-space, no-accent, lowercase) keys for all
    player names that have a cricbuzz_player_id — these are the most reliable
    canonical spellings in the DB.
    """
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT player_name FROM auction_prices_full
            WHERE year = 2026
              AND cricbuzz_player_id IS NOT NULL
              AND TRIM(cricbuzz_player_id) != ''
              AND player_name IS NOT NULL
              AND TRIM(player_name) != ''
            """
        ).fetchall()
        return {_compact_key(str(r[0])) for r in rows if r[0]}
    except sqlite3.Error:
        return set()

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def ensure_portrait_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS player_portraits (
            player_key   TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            source       TEXT NOT NULL,
            image_url    TEXT,
            content_type TEXT NOT NULL,
            image_data   BLOB NOT NULL,
            fetched_at   TEXT NOT NULL
        )
        """
    )
    _ensure_alias_schema(conn)
    ensure_identity_schema(conn)
    try:
        from espn_cricinfo import ensure_espn_schema
        ensure_espn_schema(conn)
    except Exception as exc:
        logger.debug("ESPN schema init skipped: %s", exc)
    conn.commit()

def _connect_raw() -> sqlite3.Connection:
    """Open DB without running portrait schema (avoids circular calls)."""
    return sqlite3.connect(DB_PATH)

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    ensure_portrait_schema(conn)
    return conn

# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------

def _name_exact_match(search: str, candidate: str) -> bool:
    return _normalize_key(_strip_accents(search)) == _normalize_key(_strip_accents(candidate))

def _name_loose_match(search: str, candidate: str) -> bool:
    a = _normalize_key(_strip_accents(search))
    b = _normalize_key(_strip_accents(candidate))
    if a == b:
        return True
    a_parts, b_parts = a.split(), b.split()
    if len(a_parts) < 2 or len(b_parts) < 2:
        return False
    return a_parts[-1] == b_parts[-1] and a_parts[0][0] == b_parts[0][0]

def _title_matches_player(display_name: str, title: str) -> bool:
    clean = re.sub(r"\s*\([^)]*\)", "", title).strip()
    if _name_exact_match(display_name, clean):
        return True
    a_parts = _normalize_key(_strip_accents(display_name)).split()
    b_parts = _normalize_key(_strip_accents(clean)).split()
    if len(a_parts) >= 2 and len(b_parts) >= 2:
        return a_parts[-1] == b_parts[-1] and a_parts[0] == b_parts[0]
    return False

def _search_queries(
    display_name: str,
    conn: Optional[sqlite3.Connection] = None,
    client: Optional[httpx.Client] = None,
) -> List[str]:
    """
    Build search query variants for a player name.
    Uses _resolve_display_name dynamically — no hardcoded aliases.
    """
    canonical = _resolve_display_name(display_name, conn=conn, client=client)
    out: List[str] = [display_name.strip()]
    if canonical.strip() and canonical.strip() not in out:
        out.append(canonical.strip())
    return out

# ---------------------------------------------------------------------------
# Cricbuzz ID lookup
# ---------------------------------------------------------------------------

def _lookup_cricbuzz_id(conn: sqlite3.Connection, display_name: str) -> Optional[str]:
    """
    Return Cricbuzz player_id for an exact or alias-matched name.
    Uses compact-key matching to handle minor spelling differences.
    No hardcoded name lists.
    """
    rows = conn.execute(
        """
        SELECT player_name, cricbuzz_player_id
        FROM auction_prices_full
        WHERE year = 2026
          AND cricbuzz_player_id IS NOT NULL
          AND TRIM(cricbuzz_player_id) != ''
        """
    ).fetchall()

    canonical = _resolve_display_name(display_name, conn=conn)
    keys = {
        _normalize_key(display_name),
        _normalize_key(canonical),
    }
    compacts = {_compact_key(k) for k in keys if k}

    for pname, pid in rows:
        if not pname or not pid:
            continue
        row_key = _normalize_key(str(pname))
        row_compact = _compact_key(str(pname))
        if row_key in keys or row_compact in compacts:
            return str(pid).strip()

    # Check equivalents derived from DB
    equiv_set = _load_equivalent_keys(conn)
    for pname, pid in rows:
        if not pname or not pid:
            continue
        row_key = _normalize_key(str(pname))
        for q_key in keys:
            if frozenset({q_key, row_key}) in equiv_set:
                return str(pid).strip()

    return None

def _cricbuzz_urls(player_id: str) -> List[str]:
    pid_s = quote(str(player_id).strip())
    try:
        pid_i = int(str(player_id).strip())
        bucket = (pid_i // 100) * 100  # noqa: F841
    except ValueError:
        pass
    return [
        f"https://www.cricbuzz.com/a/img/v1/i1/c{pid_s}/player.jpg",
        f"https://static.cricbuzz.com/a/img/v1/i1/c{pid_s}/player.jpg",
        f"https://www.cricbuzz.com/a/img/v1/i1/c{pid_s}/i.jpg",
        f"https://static.cricbuzz.com/a/img/v1/i1/c{pid_s}/i.jpg",
        f"https://img1.hscicdn.com/image/upload/f_auto,t_h_150_2x/lsci/players/{pid_s}_thumb.jpg",
    ]

# ---------------------------------------------------------------------------
# File / manifest helpers
# ---------------------------------------------------------------------------

def _extension_for(content_type: str) -> str:
    return {
        "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
        "image/webp": "webp", "image/gif": "gif", "image/svg+xml": "svg",
    }.get(content_type.lower(), "bin")

def _safe_file_stem(player_key: str) -> str:
    stem = re.sub(r"[^\w.-]+", "_", player_key.strip().lower())
    return stem or "player"

def _load_manifest() -> Dict[str, dict]:
    if not MANIFEST_PATH.exists():
        return {}
    try:
        payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        return dict(payload.get("players") or {})
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Could not read portrait manifest: %s", exc)
        return {}

def _save_manifest(players: Dict[str, dict]) -> None:
    PORTRAIT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "portrait_dir": str(PORTRAIT_DIR),
        "players": players,
    }
    MANIFEST_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

@lru_cache(maxsize=1)
def _suspect_local_photo_hashes() -> frozenset:
    manifest = _load_manifest()
    by_hash: Dict[str, List[str]] = {}
    for player_key, entry in manifest.items():
        if str(entry.get("source") or "") not in PHOTO_SOURCES:
            continue
        path = PORTRAIT_DIR / str(entry.get("file") or "")
        if not path.is_file() or path.suffix.lower() == ".svg":
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        by_hash.setdefault(digest, []).append(player_key)

    conn = _connect_raw()
    try:
        equiv_set = _load_equivalent_keys(conn)
    finally:
        conn.close()

    suspect: set = set()
    for digest, keys in by_hash.items():
        unique = sorted({_normalize_key(k) for k in keys})
        if len(unique) <= 1:
            continue
        if all(
            frozenset({unique[0], other}) in equiv_set
            for other in unique[1:]
        ):
            continue
        suspect.add(digest)
    return frozenset(suspect)

def _is_rejected_portrait(image_data: bytes, source: str, *, player_key: str = "") -> bool:
    if source not in PHOTO_SOURCES or not image_data:
        return False
    if hashlib.sha256(image_data).hexdigest() in _suspect_local_photo_hashes():
        return True
    if source == "cricbuzz" and len(image_data) < 2000:
        return True
    return False

def _clear_portrait_entry(conn: sqlite3.Connection, player_key: str) -> None:
    conn.execute("DELETE FROM player_portraits WHERE player_key = ?", (player_key,))
    conn.commit()
    manifest = _load_manifest()
    entry = manifest.pop(player_key, None)
    if entry:
        path = PORTRAIT_DIR / str(entry.get("file") or "")
        if path.is_file():
            path.unlink()
        _save_manifest(manifest)
        _suspect_local_photo_hashes.cache_clear()

def _write_local_portrait(
    *, player_key: str, display_name: str, source: str,
    content_type: str, image_data: bytes,
) -> Path:
    PORTRAIT_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{_safe_file_stem(player_key)}.{_extension_for(content_type)}"
    path = PORTRAIT_DIR / filename
    path.write_bytes(image_data)
    manifest = _load_manifest()
    manifest[player_key] = {
        "display_name": display_name, "source": source,
        "content_type": content_type, "file": filename, "bytes": len(image_data),
    }
    _save_manifest(manifest)
    _suspect_local_photo_hashes.cache_clear()
    return path

def _read_local_portrait(player_key: str) -> Optional[Tuple[bytes, str, str]]:
    entry = _load_manifest().get(player_key)
    if not entry:
        return None
    path = PORTRAIT_DIR / str(entry.get("file") or "")
    if not path.is_file():
        return None
    try:
        data = path.read_bytes()
        if len(data) < 20:
            return None
        source = str(entry.get("source") or "local")
        if _is_rejected_portrait(data, source, player_key=player_key):
            return None
        return data, str(entry.get("content_type") or "image/jpeg"), source
    except OSError as exc:
        logger.debug("Local portrait read failed for %s: %s", player_key, exc)
        return None

# ---------------------------------------------------------------------------
# DB cache read / write
# ---------------------------------------------------------------------------

def export_db_portraits_to_disk(conn: Optional[sqlite3.Connection] = None) -> dict:
    own_conn = conn is None
    if own_conn:
        conn = _connect()
    assert conn is not None
    stats = {"exported": 0, "skipped": 0}
    try:
        rows = conn.execute(
            "SELECT player_key, display_name, source, content_type, image_data FROM player_portraits"
        ).fetchall()
        for player_key, display_name, source, content_type, image_data in rows:
            if not image_data:
                stats["skipped"] += 1
                continue
            _write_local_portrait(
                player_key=str(player_key), display_name=str(display_name),
                source=str(source), content_type=str(content_type),
                image_data=bytes(image_data),
            )
            stats["exported"] += 1
    finally:
        if own_conn:
            conn.close()
    return stats

def portrait_cache_report(conn: Optional[sqlite3.Connection] = None) -> dict:
    own_conn = conn is None
    if own_conn:
        conn = _connect()
    assert conn is not None
    try:
        rows = conn.execute(
            "SELECT source, COUNT(*) FROM player_portraits GROUP BY source"
        ).fetchall()
        db_counts = {str(src): int(n) for src, n in rows}
        manifest = _load_manifest()
        photo_files = sum(
            1 for m in manifest.values() if str(m.get("source")) in PHOTO_SOURCES
        )
        return {
            "db": db_counts, "db_total": sum(db_counts.values()),
            "local_files": len(manifest), "local_photos": photo_files,
            "portrait_dir": str(PORTRAIT_DIR), "manifest": str(MANIFEST_PATH),
        }
    finally:
        if own_conn:
            conn.close()

def _read_cache(conn: sqlite3.Connection, player_key: str) -> Optional[Tuple[bytes, str, str]]:
    row = conn.execute(
        "SELECT image_data, content_type, source FROM player_portraits WHERE player_key = ?",
        (player_key,),
    ).fetchone()
    if not row:
        return None
    data, content_type, source = bytes(row[0]), str(row[1]), str(row[2])
    if _is_rejected_portrait(data, source, player_key=player_key):
        return None
    return data, content_type, source

def _write_cache(
    conn: sqlite3.Connection, *, player_key: str, display_name: str,
    source: str, image_url: Optional[str], content_type: str, image_data: bytes,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO player_portraits
        (player_key, display_name, source, image_url, content_type, image_data, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (player_key, display_name, source, image_url, content_type, image_data,
         datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    try:
        _write_local_portrait(
            player_key=player_key, display_name=display_name,
            source=source, content_type=content_type, image_data=image_data,
        )
    except OSError as exc:
        logger.warning("Could not write local portrait for %s: %s", display_name, exc)

def _cache_portrait(
    conn: sqlite3.Connection, *, player_key: str, display_name: str,
    source: str, image_url: Optional[str], content_type: str, image_data: bytes,
) -> Optional[Tuple[bytes, str, str]]:
    if _is_rejected_portrait(image_data, source, player_key=player_key):
        logger.debug("Rejected portrait for %s (%s, %d bytes)", display_name, source, len(image_data))
        return None
    _write_cache(
        conn, player_key=player_key, display_name=display_name,
        source=source, image_url=image_url, content_type=content_type, image_data=image_data,
    )
    return image_data, content_type, source

# ---------------------------------------------------------------------------
# TheSportsDB
# ---------------------------------------------------------------------------

def _thesportsdb_match(display_name: str, candidate: str) -> bool:
    if _name_exact_match(display_name, candidate):
        return True
    a = _normalize_key(_strip_accents(display_name)).split()
    b = _normalize_key(_strip_accents(candidate)).split()
    if len(a) < 2 or len(b) < 2 or a[-1] != b[-1]:
        return False
    if a[0] == b[0]:
        return True
    return len(b[0]) <= 2 and a[0].startswith(b[0][0])

def _thesportsdb_thumb(
    client: httpx.Client, display_name: str,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[str]:
    for query in _search_queries(display_name, conn=conn, client=client):
        try:
            resp = client.get(
                f"{THESPORTSDB_BASE}/searchplayers.php",
                params={"p": query}, headers=HTTP_HEADERS, timeout=8.0,
            )
            resp.raise_for_status()
            raw = resp.json().get("player") or []
            rows = raw if isinstance(raw, list) else [raw]
            best_url: Optional[str] = None
            best_score = -1.0
            for row in rows:
                if str(row.get("strSport") or "").lower() != "cricket":
                    continue
                cand = str(row.get("strPlayer") or "")
                resolved = _resolve_display_name(display_name, conn=conn, client=client)
                if not _thesportsdb_match(display_name, cand) and not _thesportsdb_match(resolved, cand):
                    continue
                url = row.get("strThumb") or row.get("strCutout")
                if not url:
                    continue
                score = float(row.get("relevance") or 0)
                if _normalize_key(cand) == _normalize_key(display_name):
                    score += 100
                if score > best_score:
                    best_score = score
                    best_url = str(url)
            if best_url:
                return best_url
        except Exception as exc:
            logger.debug("TheSportsDB search failed for %s: %s", query, exc)
    return None

# ---------------------------------------------------------------------------
# Wikipedia — v2 (cricket-gated, no bare name fallback)
# ---------------------------------------------------------------------------

def _wiki_title_guesses(
    display_name: str,
    conn: Optional[sqlite3.Connection] = None,
    client: Optional[httpx.Client] = None,
) -> List[str]:
    guesses: List[str] = []
    for q in _search_queries(display_name, conn=conn, client=client):
        guesses.append(q.replace(" ", "_"))
        guesses.append(f"{q} (cricketer)".replace(" ", "_"))
    return guesses

def _wiki_rest_thumb(
    client: httpx.Client, display_name: str,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[str]:
    for title in _wiki_title_guesses(display_name, conn=conn, client=client):
        try:
            resp = client.get(
                f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(title)}",
                headers=WIKI_HEADERS, timeout=8.0,
            )
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            data = resp.json()
            page_title = str(data.get("title") or "")
            resolved = _resolve_display_name(display_name, conn=conn, client=client)
            if not _title_matches_player(display_name, page_title):
                if not _title_matches_player(resolved, page_title):
                    continue
            if not _page_is_cricketer(data, display_name):
                continue
            thumb = (data.get("thumbnail") or {}).get("source")
            if thumb:
                return str(thumb)
        except Exception as exc:
            logger.debug("Wikipedia REST failed for %s (%s): %s", display_name, title, exc)
    return None

def _wiki_page_thumb(
    client: httpx.Client, page_title: str, *,
    display_name: str = "", trust_title: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[str]:
    """trust_title skips name check only — sport verification always runs."""
    title = page_title.strip()
    if not title:
        return None
    slug = title.replace(" ", "_")
    try:
        resp = client.get(
            f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(slug)}",
            headers=WIKI_HEADERS, timeout=8.0,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        resolved_title = str(data.get("title") or title)
        if display_name and not trust_title:
            resolved = _resolve_display_name(display_name, conn=conn, client=client)
            if not _title_matches_player(display_name, resolved_title):
                if not _title_matches_player(resolved, resolved_title):
                    return None
        if display_name and not _page_is_cricketer(data, display_name):
            logger.info("Wikipedia page '%s' rejected for '%s' (desc: '%s')",
                        resolved_title, display_name,
                        (data.get("description") or "")[:80])
            return None
        thumb = (data.get("thumbnail") or {}).get("source")
        return str(thumb) if thumb else None
    except Exception as exc:
        logger.debug("Wikipedia page thumb failed for %s: %s", title, exc)
        return None

def _wiki_thumb(
    client: httpx.Client, display_name: str,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[str]:
    """Wikipedia portrait search — cricket-only queries, summary-verified."""
    rest = _wiki_rest_thumb(client, display_name, conn=conn)
    if rest:
        return rest

    cricket_queries = [
        f"{q} cricketer"
        for q in _search_queries(display_name, conn=conn, client=client)
    ]

    for search in cricket_queries:
        try:
            resp = client.get(
                WIKI_API,
                params={
                    "action": "query", "generator": "search",
                    "gsrsearch": search, "gsrlimit": 5,
                    "prop": "pageimages", "piprop": "thumbnail",
                    "pithumbsize": 400, "format": "json",
                },
                headers=WIKI_HEADERS, timeout=10.0,
            )
            resp.raise_for_status()
            pages = resp.json().get("query", {}).get("pages") or {}
            best_url: Optional[str] = None
            best_score = -1.0
            for page in pages.values():
                title = str(page.get("title") or "")
                if not _title_matches_player(display_name, title):
                    continue
                thumb = (page.get("thumbnail") or {}).get("source")
                if not thumb:
                    continue
                validated_thumb = thumb
                try:
                    slug = title.replace(" ", "_")
                    sum_resp = client.get(
                        f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(slug)}",
                        headers=WIKI_HEADERS, timeout=6.0,
                    )
                    if sum_resp.status_code == 200:
                        sum_data = sum_resp.json()
                        if not _page_is_cricketer(sum_data, display_name):
                            continue
                        sum_thumb = (sum_data.get("thumbnail") or {}).get("source")
                        if sum_thumb:
                            validated_thumb = sum_thumb
                except Exception:
                    pass
                score = 0.0
                clean_title = re.sub(r"\s*\([^)]*\)", "", title).strip()
                if _normalize_key(clean_title) == _normalize_key(display_name):
                    score += 120
                if "cricketer" in title.lower() or "cricket" in title.lower():
                    score += 40
                if score > best_score:
                    best_score = score
                    best_url = validated_thumb
            if best_url:
                return best_url
        except Exception as exc:
            logger.debug("Wikipedia search failed for %s (%s): %s", display_name, search, exc)

    # Direct lookup — cricket-qualified slugs only
    cricket_slugs = [
        t for t in _wiki_title_guesses(display_name, conn=conn, client=client)
        if "cricketer" in t.lower() or "cricket" in t.lower()
    ]
    for title in cricket_slugs:
        try:
            resp = client.get(
                WIKI_API,
                params={
                    "action": "query", "titles": title.replace("_", " "),
                    "prop": "pageimages", "piprop": "thumbnail",
                    "pithumbsize": 400, "format": "json",
                },
                headers=WIKI_HEADERS, timeout=8.0,
            )
            resp.raise_for_status()
            pages = resp.json().get("query", {}).get("pages") or {}
            for page in pages.values():
                if page.get("missing") is not None:
                    continue
                page_title = str(page.get("title") or "")
                if not _title_matches_player(display_name, page_title):
                    continue
                thumb = (page.get("thumbnail") or {}).get("source")
                if not thumb:
                    continue
                try:
                    slug = page_title.replace(" ", "_")
                    sum_resp = client.get(
                        f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(slug)}",
                        headers=WIKI_HEADERS, timeout=6.0,
                    )
                    if sum_resp.status_code == 200:
                        sum_data = sum_resp.json()
                        if not _page_is_cricketer(sum_data, display_name):
                            continue
                        better = (sum_data.get("thumbnail") or {}).get("source")
                        if better:
                            thumb = better
                except Exception:
                    pass
                return str(thumb)
        except Exception as exc:
            logger.debug("Wikipedia direct title failed for %s: %s", display_name, exc)
    return None

# ---------------------------------------------------------------------------
# Image download
# ---------------------------------------------------------------------------

def _download_image(
    client: httpx.Client, url: str, *,
    headers: Optional[dict] = None, retries: int = 3,
) -> Optional[Tuple[bytes, str]]:
    hdrs = headers or HTTP_HEADERS
    for attempt in range(retries):
        try:
            resp = client.get(url, headers=hdrs, timeout=15.0)
            if resp.status_code in (403, 429) and attempt + 1 < retries:
                time.sleep(2.0 * (attempt + 1))
                continue
            resp.raise_for_status()
            data = resp.content
            if len(data) < 400:
                return None
            ct = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
            if not ct.startswith("image/"):
                return None
            return data, ct
        except Exception as exc:
            logger.debug("Image download failed %s (attempt %s): %s", url, attempt + 1, exc)
            if attempt + 1 < retries:
                time.sleep(0.5 * (attempt + 1))
    return None

# ---------------------------------------------------------------------------
# Main source orchestrator
# ---------------------------------------------------------------------------

def _try_sources(
    client: httpx.Client, conn: sqlite3.Connection,
    *, player_key: str, display_name: str,
) -> Optional[Tuple[bytes, str, str]]:
    profile = resolve_cricketer_profile(
        conn, display_name,
        resolve_display_name=lambda n: _resolve_display_name(n, conn=conn),
        lookup_cricbuzz_id=_lookup_cricbuzz_id,
    )
    if not profile.photo_eligible:
        logger.info("Portrait blocked for %s (%s, conf=%s)",
                    display_name, profile.verification_source, profile.confidence)
        return None

    search_name = profile.canonical_name or _resolve_display_name(display_name, conn=conn, client=client)

    # Deduplicated lookup name list
    lookup_names: List[str] = []
    seen: set = set()
    for candidate in (search_name, display_name, _resolve_display_name(display_name, conn=conn, client=client)):
        k = _normalize_key(candidate)
        if k and k not in seen:
            seen.add(k)
            lookup_names.append(candidate)

    # 1. ESPN Cricinfo (uniform headshots) — optional; often blocked without browser token
    if not _skip_espn_portraits():
        try:
            from espn_cricinfo import fetch_and_store_espn_portrait
            espn_hit = fetch_and_store_espn_portrait(conn, client, display_name, force=False)
            if espn_hit:
                return espn_hit
            if search_name != display_name:
                espn_hit = fetch_and_store_espn_portrait(conn, client, search_name, force=False)
                if espn_hit:
                    return espn_hit
        except Exception as exc:
            logger.debug("ESPN portrait fetch failed for %s: %s", display_name, exc)

    # 2. Cricbuzz
    cric_id = None
    for candidate in lookup_names:
        cric_id = _lookup_cricbuzz_id(conn, candidate)
        if cric_id:
            break
    if cric_id:
        for url in _cricbuzz_urls(cric_id):
            got = _download_image(client, url, headers=CRICBUZZ_HEADERS)
            if got:
                cached = _cache_portrait(
                    conn, player_key=player_key, display_name=display_name,
                    source="cricbuzz", image_url=url,
                    content_type=got[1], image_data=got[0],
                )
                if cached:
                    return cached

    # 3. TheSportsDB
    sports_url = _thesportsdb_thumb(client, search_name, conn=conn)
    for candidate in lookup_names[1:]:
        if sports_url:
            break
        sports_url = _thesportsdb_thumb(client, candidate, conn=conn)
    if sports_url:
        got = _download_image(client, sports_url)
        if got:
            cached = _cache_portrait(
                conn, player_key=player_key, display_name=display_name,
                source="thesportsdb", image_url=sports_url,
                content_type=got[1], image_data=got[0],
            )
            if cached:
                return cached

    # 4. Wikipedia
    wiki_url: Optional[str] = None
    if profile.wikipedia_title:
        wiki_url = _wiki_page_thumb(
            client, profile.wikipedia_title,
            display_name=search_name,
            trust_title=profile.groq_verified or profile.confidence == "high",
            conn=conn,
        )
    if not wiki_url:
        for candidate in lookup_names:
            wiki_url = _wiki_thumb(client, candidate, conn=conn)
            if wiki_url:
                break
    if wiki_url:
        got = _download_image(client, wiki_url)
        if got:
            cached = _cache_portrait(
                conn, player_key=player_key, display_name=display_name,
                source="wikipedia", image_url=wiki_url,
                content_type=got[1], image_data=got[0],
            )
            if cached:
                return cached

    return None


def fetch_cricbuzz_portrait_only(
    conn: sqlite3.Connection,
    client: httpx.Client,
    display_name: str,
    *,
    force: bool = False,
) -> Optional[Tuple[bytes, str, str]]:
    """
    Fetch portrait from Cricbuzz CDN using cricbuzz_player_id in auction_prices_full.
    Does not call ESPN, TheSportsDB, or Wikipedia.
    """
    clean = display_name.strip()
    if not clean:
        return None
    player_key = _normalize_key(clean)
    if not force:
        cached = _read_cache(conn, player_key)
        if cached and cached[2] == "cricbuzz" and _use_cached_photo(cached):
            return cached

    lookup_names: List[str] = []
    seen: set = set()
    for candidate in (
        clean,
        _resolve_display_name(clean, conn=conn, client=client),
    ):
        k = _normalize_key(candidate)
        if k and k not in seen:
            seen.add(k)
            lookup_names.append(candidate)

    cric_id: Optional[str] = None
    for candidate in lookup_names:
        cric_id = _lookup_cricbuzz_id(conn, candidate)
        if cric_id:
            break
    if not cric_id:
        return None

    for url in _cricbuzz_urls(cric_id):
        got = _download_image(client, url, headers=CRICBUZZ_HEADERS)
        if got:
            return _cache_portrait(
                conn,
                player_key=player_key,
                display_name=clean,
                source="cricbuzz",
                image_url=url,
                content_type=got[1],
                image_data=got[0],
            )
    return None


def _resolve_portrait_with_client(
    client: httpx.Client, conn: sqlite3.Connection, display_name: str,
) -> Tuple[bytes, str, str]:
    clean = display_name.strip()
    player_key = _normalize_key(clean)
    resolved = _try_sources(client, conn, player_key=player_key, display_name=clean)
    if resolved:
        return resolved
    svg = initials_svg(clean)
    _write_cache(conn, player_key=player_key, display_name=clean, source="initials",
                 image_url=None, content_type="image/svg+xml", image_data=svg)
    return svg, "image/svg+xml", "initials"

def _network_fetch_enabled() -> bool:
    """When false, only DB/disk cache is used — misses return initials (no auto-download)."""
    return os.getenv("PORTRAIT_FETCH_ON_REQUEST", "1").strip() not in ("0", "false", "no")


def fetch_and_cache_portrait(
    display_name: str, *, conn: Optional[sqlite3.Connection] = None,
) -> Tuple[bytes, str, str]:
    clean = display_name.strip()
    if not clean:
        return initials_svg("?"), "image/svg+xml", "initials"
    player_key = _normalize_key(clean)
    own_conn = conn is None
    if own_conn:
        conn = _connect()
    assert conn is not None
    try:
        cached = _read_cache(conn, player_key)
        if _use_cached_photo(cached):
            return cached
        local = _read_local_portrait(player_key)
        if local and local[2] in PHOTO_SOURCES:
            if local[2] == "espncricinfo" or not _prefer_espn_portraits():
                data, ct, source = local
                _write_cache(conn, player_key=player_key, display_name=clean,
                             source=source, image_url=None, content_type=ct, image_data=data)
                return data, ct, source
        if _network_fetch_enabled():
            with httpx.Client(follow_redirects=True, trust_env=False) as client:
                resolved = _try_sources(client, conn, player_key=player_key, display_name=clean)
                if resolved:
                    return resolved
        if cached and cached[2] == "initials":
            return cached
        svg = initials_svg(clean)
        _write_cache(conn, player_key=player_key, display_name=clean, source="initials",
                     image_url=None, content_type="image/svg+xml", image_data=svg)
        return svg, "image/svg+xml", "initials"
    finally:
        if own_conn:
            conn.close()

@lru_cache(maxsize=512)
def portrait_image_bytes(display_name: str) -> Tuple[bytes, str, str]:
    return fetch_and_cache_portrait(display_name)

# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------

def purge_rejected_portraits(conn: Optional[sqlite3.Connection] = None) -> dict:
    own_conn = conn is None
    if own_conn:
        conn = _connect()
    assert conn is not None
    removed: List[str] = []
    try:
        manifest = _load_manifest()
        rows = conn.execute(
            "SELECT player_key, display_name, source, image_data FROM player_portraits"
        ).fetchall()
        for player_key, display_name, source, image_data in rows:
            src = str(source)
            if src not in PHOTO_SOURCES:
                continue
            if not _is_rejected_portrait(bytes(image_data), src, player_key=str(player_key)):
                continue
            conn.execute("DELETE FROM player_portraits WHERE player_key = ?", (player_key,))
            entry = manifest.pop(str(player_key), None)
            if entry:
                path = PORTRAIT_DIR / str(entry.get("file") or "")
                if path.is_file():
                    path.unlink()
            removed.append(str(display_name))
        if removed:
            conn.commit()
            _save_manifest(manifest)
            _suspect_local_photo_hashes.cache_clear()
            portrait_image_bytes.cache_clear()
    finally:
        if own_conn:
            conn.close()
    return {"removed": len(removed), "players": removed}

def wipe_portrait_cache_completely(conn: Optional[sqlite3.Connection] = None) -> dict:
    own_conn = conn is None
    if own_conn:
        conn = _connect()
    assert conn is not None
    stats = {"db_portraits": 0, "db_identity": 0, "disk_files": 0}
    try:
        ensure_identity_schema(conn)
        stats["db_portraits"] = int(
            conn.execute("SELECT COUNT(*) FROM player_portraits").fetchone()[0])
        stats["db_identity"] = int(
            conn.execute("SELECT COUNT(*) FROM player_identity_cache").fetchone()[0])
        conn.execute("DELETE FROM player_portraits")
        conn.execute("DELETE FROM player_identity_cache")
        conn.execute("DELETE FROM player_name_aliases")
        conn.execute("DELETE FROM player_name_equivalents")
        try:
            conn.execute("DELETE FROM player_espn_cricinfo")
        except sqlite3.OperationalError:
            pass
        conn.commit()
        PORTRAIT_DIR.mkdir(parents=True, exist_ok=True)
        for path in PORTRAIT_DIR.iterdir():
            if path.is_file() and path.name != ".gitkeep":
                path.unlink()
                stats["disk_files"] += 1
        _save_manifest({})
        _suspect_local_photo_hashes.cache_clear()
        portrait_image_bytes.cache_clear()
        _load_canonical_index_cached.cache_clear()
    finally:
        if own_conn:
            conn.close()
    return stats

def portrait_metadata(display_name: str) -> dict:
    _, content_type, source = portrait_image_bytes(display_name)
    key = _normalize_key(display_name)
    entry = _load_manifest().get(key) or {}
    local_file = entry.get("file")
    conn = _connect()
    try:
        profile = resolve_cricketer_profile(
            conn, display_name.strip(),
            resolve_display_name=lambda n: _resolve_display_name(n, conn=conn),
            lookup_cricbuzz_id=_lookup_cricbuzz_id,
        )
    finally:
        conn.close()
    return {
        "player_name": display_name.strip(),
        "source": source, "content_type": content_type,
        "has_photo": source in PHOTO_SOURCES,
        "img_url": f"/api/players/avatar/img?name={display_name.strip()}",
        "local_file": str(PORTRAIT_DIR / local_file) if local_file else None,
        "identity": {
            "is_cricketer": profile.is_cricketer,
            "canonical_name": profile.canonical_name,
            "wikipedia_title": profile.wikipedia_title,
            "verification_source": profile.verification_source,
            "confidence": profile.confidence,
            "groq_verified": profile.groq_verified,
        },
    }

def collect_portrait_names() -> List[str]:
    names: set = set()
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT player_name FROM auction_prices_full
            WHERE year = 2026 AND player_name IS NOT NULL AND TRIM(player_name) != ''
            """
        ).fetchall()
        names.update(str(r[0]).strip() for r in rows if r[0])
    except Exception as exc:
        logger.debug("Could not load auction_prices_full names: %s", exc)
    finally:
        conn.close()
    try:
        from csk_squad_catalog import load_official_csk_squad_2026
        for row in load_official_csk_squad_2026():
            names.add(str(row.get("name") or "").strip())
    except Exception as exc:
        logger.debug("Could not load CSK squad names: %s", exc)
    return sorted(n for n in names if n)

def warm_portrait_cache(
    names: Optional[Iterable[str]] = None, *,
    force_initials: bool = False,
    refresh_all: bool = False,
    delay_s: float = 0.15,
    show_progress: bool = False,
) -> dict:
    targets = list(names) if names is not None else collect_portrait_names()
    total = len(targets)
    conn = _connect()

    # Build equivalent-key groups from the DB before fetching
    _rebuild_equivalent_keys(conn)
    _load_canonical_index_cached.cache_clear()

    stats = {
        "total": total, "espncricinfo": 0, "cricbuzz": 0, "thesportsdb": 0, "wikipedia": 0,
        "initials": 0, "cached": 0, "upgraded": 0, "skipped_good": 0,
        "portrait_dir": str(PORTRAIT_DIR),
    }
    try:
        with httpx.Client(follow_redirects=True, trust_env=False) as client:
            for idx, name in enumerate(targets, start=1):
                key = _normalize_key(name)
                cached = _read_cache(conn, key)

                if _use_cached_photo(cached) and not refresh_all:
                    stats["cached"] += 1
                    stats["skipped_good"] += 1
                    continue

                if not refresh_all and force_initials:
                    if not cached or cached[2] != "initials":
                        if _use_cached_photo(cached):
                            stats["cached"] += 1
                            stats["skipped_good"] += 1
                        continue

                was_initials = cached and cached[2] == "initials"
                if cached:
                    _clear_portrait_entry(conn, key)

                _, _, source = _resolve_portrait_with_client(client, conn, name)
                if source in PHOTO_SOURCES:
                    stats[source] = stats.get(source, 0) + 1
                    if was_initials:
                        stats["upgraded"] += 1
                else:
                    stats["initials"] += 1

                if show_progress and (idx == 1 or idx % 10 == 0 or idx == total):
                    photos = (
                        stats["espncricinfo"] + stats["cricbuzz"]
                        + stats["thesportsdb"] + stats["wikipedia"]
                    )
                    print(
                        f"  [{idx}/{total}] +{stats['upgraded']} upgraded, "
                        f"{photos} new photos, {stats['initials']} still initials …",
                        flush=True,
                    )

                if delay_s > 0:
                    time.sleep(delay_s)
    finally:
        conn.close()

    portrait_image_bytes.cache_clear()
    stats["report"] = portrait_cache_report()
    logger.info(
        "Portrait cache warm: espn=%s cricbuzz=%s sportsdb=%s wiki=%s | "
        "upgraded=%s kept=%s initials=%s (of %s)",
        stats["espncricinfo"], stats["cricbuzz"], stats["thesportsdb"], stats["wikipedia"],
        stats["upgraded"], stats["cached"], stats["initials"], stats["total"],
    )
    return stats