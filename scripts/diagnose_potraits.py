#!/usr/bin/env python3
"""
Portrait cache diagnostics (DB + manifest).

Run from repo root:
  python3 scripts/diagnose_potraits.py
  python3 scripts/diagnose_potraits.py "Jamie Overton"
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "auction_data.db"
MANIFEST = ROOT / "data" / "player_portraits" / "manifest.json"

API_DIR = ROOT / "api"
sys.path.insert(0, str(API_DIR))


def _normalize_key(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _load_manifest() -> dict:
    if not MANIFEST.exists():
        return {}
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    players = payload.get("players")
    return players if isinstance(players, dict) else payload


def _run_batch(conn: sqlite3.Connection, manifest: dict) -> None:
    print("=== 1. Players missing cricbuzz_player_id (2026) ===")
    rows = conn.execute(
        """
        SELECT DISTINCT player_name
        FROM auction_prices_full
        WHERE year = 2026
          AND (cricbuzz_player_id IS NULL OR TRIM(cricbuzz_player_id) = '')
        ORDER BY player_name
        """
    ).fetchall()
    print(f"Count: {len(rows)}")
    for (name,) in rows:
        print(f"  {name}")

    print("\n=== 2. Manifest coverage ===")
    real = [k for k, v in manifest.items() if not str(v.get("file", "")).endswith(".svg")]
    initials = [k for k, v in manifest.items() if str(v.get("file", "")).endswith(".svg")]
    print(f"  Total entries : {len(manifest)}")
    print(f"  Real photos   : {len(real)}")
    print(f"  Initials SVG  : {len(initials)}")

    by_source: dict[str, int] = {}
    for entry in manifest.values():
        src = str(entry.get("source") or "unknown")
        by_source[src] = by_source.get(src, 0) + 1
    print("  By source:")
    for src, n in sorted(by_source.items(), key=lambda x: -x[1]):
        print(f"    {src}: {n}")

    print("\n=== 3. DB portrait cache (player_portraits) ===")
    try:
        db_rows = conn.execute(
            "SELECT source, COUNT(*) FROM player_portraits GROUP BY source ORDER BY 2 DESC"
        ).fetchall()
        for src, n in db_rows:
            print(f"    {src}: {n}")
    except sqlite3.Error as exc:
        print(f"    (table missing?) {exc}")

    print("\n=== 4. 2026 pool players with NO manifest entry ===")
    pool = conn.execute(
        """
        SELECT DISTINCT player_name, cricbuzz_player_id
        FROM auction_prices_full
        WHERE year = 2026
        ORDER BY player_name
        """
    ).fetchall()
    not_in_manifest = [
        (n, pid) for n, pid in pool if _normalize_key(n) not in manifest
    ]
    print(f"Count: {len(not_in_manifest)} / {len(pool)}")
    for n, pid in not_in_manifest[:40]:
        print(f"  {n:<30}  cricbuzz_id={pid or 'MISSING'}")
    if len(not_in_manifest) > 40:
        print(f"  … and {len(not_in_manifest) - 40} more")

    print("\n=== 5. Sample manifest keys (first 10) ===")
    for k in list(manifest.keys())[:10]:
        e = manifest[k]
        print(f"  {k:<35} → {e.get('file', '?')} ({e.get('source', '?')})")


def _run_one(name: str) -> None:
    from player_portrait_store import (  # noqa: E402
        _lookup_cricbuzz_id,
        _normalize_key,
        _resolve_display_name,
        portrait_metadata,
    )

    conn = sqlite3.connect(DB)
    try:
        canonical = _resolve_display_name(name, conn=conn)
        pid = _lookup_cricbuzz_id(conn, name) or _lookup_cricbuzz_id(conn, canonical)
        meta = portrait_metadata(name)
        key = _normalize_key(name)
        manifest = _load_manifest()
        entry = manifest.get(key)
        print(f"Player:     {name}")
        print(f"Key:        {key}")
        print(f"Canonical:  {canonical}")
        print(f"Cricbuzz:   {pid or 'none'}")
        print(f"Portrait:   {meta.get('source')} ({meta.get('content_type')})")
        print(f"Has photo:  {meta.get('has_photo')}")
        if entry:
            print(f"Manifest:   {entry.get('file')} [{entry.get('source')}]")
        else:
            print("Manifest:   (missing)")
        ident = meta.get("identity") or {}
        print(
            f"Identity:   {ident.get('verification_source')} "
            f"conf={ident.get('confidence')} groq={ident.get('groq_verified')}"
        )
    finally:
        conn.close()


def main() -> None:
    if len(sys.argv) > 1:
        _run_one(sys.argv[1].strip())
        return

    conn = sqlite3.connect(DB)
    try:
        manifest = _load_manifest()
        _run_batch(conn, manifest)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
