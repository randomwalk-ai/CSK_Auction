"""
Player portraits — IPL face card when present; ESPN only for players without one.
"""

from __future__ import annotations

from typing import Optional, Tuple

from espn_cricinfo import lookup_espn_mapping
from ipl_facecards import has_ipl_facecard, lookup_facecard
from player_portrait_store import (
    _connect,
    _normalize_key,
    export_db_portraits_to_disk,
    initials_svg,
    portrait_cache_report,
    portrait_image_bytes,
    warm_portrait_cache,
)

CDN_SOURCES = frozenset({"iplt20", "espncricinfo"})


def _resolve_espn_url(conn, player_key: str) -> Optional[str]:
    mapping = lookup_espn_mapping(conn, player_key)
    if not mapping:
        return None
    uniform = str(mapping[1] or "").strip()
    if uniform.startswith("https://"):
        return uniform
    return None


def resolve_avatar(player_name: str, *, skip_ipl: bool = False) -> Tuple[str, str]:
    """
    Return (image_url_or_empty, source).
    IPL face card when stored; ESPN only if no face card; else initials.
    skip_ipl=True is used after a broken IPL CDN load — still no ESPN if face card exists.
    """
    clean = player_name.strip()
    if not clean:
        return "", "initials"
    player_key = _normalize_key(clean)
    conn = _connect()
    try:
        has_fc = has_ipl_facecard(conn, clean)
        if not skip_ipl and has_fc:
            row = lookup_facecard(conn, clean)
            url = str(row.get("facecard_url") or "").strip()
            if url:
                return url, "iplt20"
        if has_fc:
            return "", "initials"
        espn_url = _resolve_espn_url(conn, player_key)
        if espn_url:
            return espn_url, "espncricinfo"
    finally:
        conn.close()
    return "", "initials"


def avatar_image_bytes(player_name: str) -> Tuple[bytes, str, str]:
    """Bytes for /avatar/img — ESPN fetch only when no IPL face card."""
    clean = player_name.strip()
    if not clean:
        return initials_svg("?"), "image/svg+xml", "initials"
    conn = _connect()
    try:
        if has_ipl_facecard(conn, clean):
            return initials_svg(clean), "image/svg+xml", "initials"
    finally:
        conn.close()
    return portrait_image_bytes(clean)


__all__ = [
    "avatar_image_bytes",
    "CDN_SOURCES",
    "export_db_portraits_to_disk",
    "initials_svg",
    "portrait_cache_report",
    "resolve_avatar",
    "warm_portrait_cache",
]
