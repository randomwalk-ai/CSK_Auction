"""
Cricketer profile verification before portrait fetch.

When GROQ_API_KEY is set, Groq runs for every portrait lookup (cached in SQLite).
Auction-pool membership is passed to Groq as ground truth; pool still confirms
cricketers if Groq is unavailable or disagrees on a listed player.

Portrait fetch order after verification:
  verify → Cricbuzz → TheSportsDB → Wikipedia
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from groq_player_identity import GroqIdentityResult, verify_cricketer_via_groq

logger = logging.getLogger(__name__)


def _groq_api_key() -> Optional[str]:
    try:
        from env_loader import groq_api_key

        return groq_api_key()
    except ImportError:
        key = (os.getenv("GROQ_API_KEY") or "").strip()
        return key or None


@dataclass(frozen=True)
class CricketerProfile:
    is_cricketer: bool
    canonical_name: str
    wikipedia_title: str
    verification_source: str
    confidence: str
    source_note: str = ""
    groq_verified: bool = False

    @property
    def photo_eligible(self) -> bool:
        if not self.is_cricketer:
            return False
        if self.confidence not in ("high", "medium"):
            return False
        if os.getenv("PORTRAIT_REQUIRE_GROQ", "0") != "1":
            return True
        # Strict: must have a successful Groq check (groq+pool or groq-only)
        if self.groq_verified:
            return True
        # IPL auction pool is authoritative for this app — allow when Groq is skipped or rate-limited
        if self.verification_source == "ipl_auction_pool" and self.confidence == "high":
            return True
        if (
            self.verification_source == "ipl_auction_pool"
            and "Groq API unavailable" in (self.source_note or "")
        ):
            return True
        return False


def ensure_identity_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS player_identity_cache (
            player_key TEXT PRIMARY KEY,
            query_name TEXT NOT NULL,
            is_cricketer INTEGER NOT NULL,
            canonical_name TEXT,
            wikipedia_title TEXT,
            verification_source TEXT NOT NULL,
            confidence TEXT NOT NULL,
            source_note TEXT,
            verified_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _normalize_key(name: str) -> str:
    import re

    return re.sub(r"\s+", " ", name.strip().lower())


def _read_identity_cache(conn: sqlite3.Connection, player_key: str) -> Optional[CricketerProfile]:
    ensure_identity_schema(conn)
    row = conn.execute(
        """
        SELECT is_cricketer, canonical_name, wikipedia_title,
               verification_source, confidence, source_note
        FROM player_identity_cache WHERE player_key = ?
        """,
        (player_key,),
    ).fetchone()
    if not row:
        return None
    source = str(row[3] or "")
    return CricketerProfile(
        is_cricketer=bool(row[0]),
        canonical_name=str(row[1] or ""),
        wikipedia_title=str(row[2] or ""),
        verification_source=source,
        confidence=str(row[4] or "low"),
        source_note=str(row[5] or ""),
        groq_verified="groq" in source,
    )


def _write_identity_cache(
    conn: sqlite3.Connection,
    *,
    player_key: str,
    query_name: str,
    profile: CricketerProfile,
) -> None:
    ensure_identity_schema(conn)
    conn.execute(
        """
        INSERT OR REPLACE INTO player_identity_cache
        (player_key, query_name, is_cricketer, canonical_name, wikipedia_title,
         verification_source, confidence, source_note, verified_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            player_key,
            query_name,
            1 if profile.is_cricketer else 0,
            profile.canonical_name or query_name,
            profile.wikipedia_title or "",
            profile.verification_source,
            profile.confidence,
            profile.source_note or "",
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.commit()


def _verify_via_auction_pool(
    conn: sqlite3.Connection,
    display_name: str,
    *,
    resolve_display_name,
    lookup_cricbuzz_id,
) -> Optional[CricketerProfile]:
    """Player listed in IPL 2026 auction pool is treated as a verified cricketer."""
    canonical = resolve_display_name(display_name)
    keys = {_normalize_key(display_name), _normalize_key(canonical)}
    keys.discard("")

    try:
        rows = conn.execute(
            """
            SELECT DISTINCT player_name
            FROM auction_prices_full
            WHERE year = 2026
              AND player_name IS NOT NULL
              AND TRIM(player_name) != ''
            """
        ).fetchall()
    except sqlite3.Error as exc:
        logger.debug("Auction pool identity check failed: %s", exc)
        return None

    matched_name = ""
    for (pname,) in rows:
        pool_name = str(pname).strip()
        row_key = _normalize_key(pool_name)
        row_canonical = _normalize_key(resolve_display_name(pool_name))
        if row_key in keys or row_canonical in keys:
            matched_name = resolve_display_name(pool_name) or pool_name
            break

    if not matched_name:
        pid = lookup_cricbuzz_id(conn, display_name) or lookup_cricbuzz_id(conn, canonical)
        if not pid:
            return None
        matched_name = canonical or display_name.strip()

    wiki_hint = ""
    repaired = resolve_display_name(matched_name) or matched_name
    if repaired:
        wiki_hint = f"{repaired} (cricketer)"

    return CricketerProfile(
        is_cricketer=True,
        canonical_name=matched_name or canonical or display_name.strip(),
        wikipedia_title=wiki_hint,
        verification_source="ipl_auction_pool",
        confidence="high",
        source_note="Listed in IPL 2026 auction pool",
        groq_verified=False,
    )


def _merge_pool_and_groq(
    pool: Optional[CricketerProfile],
    groq: GroqIdentityResult,
    query: str,
) -> CricketerProfile:
    """Combine auction DB ground truth with Groq canonical name / Wikipedia hints."""
    if pool and not groq.api_ok:
        if os.getenv("GROQ_IDENTITY_LOG_POOL_FALLBACK", "0") == "1":
            logger.warning(
                "Groq API unavailable for %s — using IPL auction pool identity", query,
            )
        else:
            logger.debug(
                "Groq API unavailable for %s — using IPL auction pool identity", query,
            )
        return CricketerProfile(
            is_cricketer=True,
            canonical_name=pool.canonical_name or query,
            wikipedia_title=pool.wikipedia_title,
            verification_source="ipl_auction_pool",
            confidence=pool.confidence,
            source_note=f"{pool.source_note}; Groq API unavailable",
            groq_verified=False,
        )

    if pool and groq.is_cricketer:
        conf = "high"
        if groq.confidence == "low":
            conf = pool.confidence
        notes = [n for n in (pool.source_note, groq.source_note) if n]
        return CricketerProfile(
            is_cricketer=True,
            canonical_name=groq.canonical_name or pool.canonical_name or query,
            wikipedia_title=groq.wikipedia_title or pool.wikipedia_title,
            verification_source="groq+ipl_auction_pool",
            confidence=conf,
            source_note="; ".join(notes),
            groq_verified=True,
        )

    if pool and not groq.is_cricketer and groq.api_ok:
        logger.info(
            "Groq rejected %s as cricketer but IPL auction pool confirms — trusting pool",
            query,
        )
        return CricketerProfile(
            is_cricketer=True,
            canonical_name=pool.canonical_name or query,
            wikipedia_title=pool.wikipedia_title,
            verification_source="ipl_auction_pool",
            confidence=pool.confidence,
            source_note=f"{pool.source_note}; Groq disagreed",
            groq_verified=False,
        )

    if groq.is_cricketer and groq.confidence in ("high", "medium"):
        return CricketerProfile(
            is_cricketer=True,
            canonical_name=groq.canonical_name or query,
            wikipedia_title=groq.wikipedia_title,
            verification_source="groq",
            confidence=groq.confidence,
            source_note=groq.source_note,
            groq_verified=True,
        )

    if pool:
        return pool

    return CricketerProfile(
        is_cricketer=False,
        canonical_name=groq.canonical_name or query,
        wikipedia_title="",
        verification_source="groq" if groq.source_note else "unverified",
        confidence="low",
        source_note=groq.source_note or "Not verified as cricketer",
        groq_verified=bool(groq.source_note),
    )


def resolve_cricketer_profile(
    conn: sqlite3.Connection,
    display_name: str,
    *,
    resolve_display_name,
    lookup_cricbuzz_id,
    groq_api_key: Optional[str] = None,
    use_cache: bool = True,
    force_groq: bool = False,
) -> CricketerProfile:
    """
    Verify whether a query name is a cricketer before any portrait download.
    Groq is consulted when GROQ_API_KEY is set (unless cached).
    """
    query = display_name.strip()
    if not query:
        return CricketerProfile(
            is_cricketer=False,
            canonical_name="",
            wikipedia_title="",
            verification_source="empty",
            confidence="low",
        )

    player_key = _normalize_key(query)
    require_groq = os.getenv("PORTRAIT_REQUIRE_GROQ", "0") == "1"
    if use_cache and not force_groq:
        cached = _read_identity_cache(conn, player_key)
        if cached and (not require_groq or cached.groq_verified or "Groq API unavailable" in (cached.source_note or "")):
            return cached

    canonical = resolve_display_name(query)
    cric_id = lookup_cricbuzz_id(conn, query) or lookup_cricbuzz_id(conn, canonical)
    pool_hit = _verify_via_auction_pool(
        conn,
        query,
        resolve_display_name=resolve_display_name,
        lookup_cricbuzz_id=lookup_cricbuzz_id,
    )

    skip_groq_for_pool = os.getenv("GROQ_SKIP_FOR_POOL", "1") == "1"
    if skip_groq_for_pool and pool_hit:
        _write_identity_cache(conn, player_key=player_key, query_name=query, profile=pool_hit)
        return pool_hit

    groq_key = groq_api_key if groq_api_key is not None else _groq_api_key()
    if groq_key:
        groq_hit = verify_cricketer_via_groq(
            groq_key,
            query,
            context="IPL 2026 auction player pool portrait lookup",
            cricbuzz_player_id=cric_id,
            in_auction_pool=pool_hit is not None,
        )
        profile = _merge_pool_and_groq(pool_hit, groq_hit, query)
        _write_identity_cache(conn, player_key=player_key, query_name=query, profile=profile)
        delay = float(os.getenv("GROQ_IDENTITY_DELAY_S", "1.0") or "1.0")
        if delay > 0:
            import time

            time.sleep(delay)
        return profile

    if pool_hit:
        _write_identity_cache(conn, player_key=player_key, query_name=query, profile=pool_hit)
        return pool_hit

    profile = CricketerProfile(
        is_cricketer=False,
        canonical_name=canonical or query,
        wikipedia_title="",
        verification_source="unverified",
        confidence="low",
        source_note="Set GROQ_API_KEY in .env for Groq identity verification",
    )
    _write_identity_cache(conn, player_key=player_key, query_name=query, profile=profile)
    return profile
