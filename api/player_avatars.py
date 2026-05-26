"""
Player portraits — IPL face-card URLs (primary) or initials fallback.
"""

from __future__ import annotations

from typing import Tuple

from player_portrait_store import (
    _connect,
    _normalize_key,
    export_db_portraits_to_disk,
    initials_svg,
    portrait_cache_report,
    warm_portrait_cache,
)
from ipl_facecards import lookup_facecard


def resolve_avatar(player_name: str) -> Tuple[str, str]:
    """Return (image_url_or_empty, source). source is iplt20 or initials."""
    clean = player_name.strip()
    conn = _connect()
    try:
        row = lookup_facecard(conn, clean)
        if row and row.get("facecard_url"):
            return str(row["facecard_url"]), "iplt20"
    finally:
        conn.close()
    return "", "initials"


def avatar_image_bytes(player_name: str) -> Tuple[bytes, str, str]:
    """Same-origin bytes — initials SVG when no IPL face card is stored."""
    clean = player_name.strip()
    url, source = resolve_avatar(clean)
    if source == "iplt20" and url:
        # Client should load CDN URL directly; proxy returns initials if called.
        pass
    data = initials_svg(clean)
    return data, "image/svg+xml", "initials"


__all__ = [
    "avatar_image_bytes",
    "export_db_portraits_to_disk",
    "initials_svg",
    "portrait_cache_report",
    "resolve_avatar",
    "warm_portrait_cache",
]
