#!/usr/bin/env python3
"""Fix PDF-spacing artifacts in auction_prices_full (2026) — no hardcoded name list."""

from __future__ import annotations

import sys
from pathlib import Path

API_DIR = Path(__file__).resolve().parent.parent / "api"
sys.path.insert(0, str(API_DIR))

from player_portrait_store import (  # noqa: E402
    _connect,
    _load_canonical_index,
    _normalize_key,
    _rebuild_equivalent_keys,
    _repair_pdf_name,
    _write_alias_cache,
)


def main() -> None:
    conn = _connect()
    try:
        _rebuild_equivalent_keys(conn)
        index = _load_canonical_index(conn)
        rows = conn.execute(
            """
            SELECT rowid, player_name FROM auction_prices_full
            WHERE year = 2026
              AND player_name IS NOT NULL
              AND TRIM(player_name) != ''
            """
        ).fetchall()
        updated = 0
        for rowid, raw in rows:
            name = str(raw).strip()
            repaired = _repair_pdf_name(name, index, conn=conn)
            if _normalize_key(repaired) == _normalize_key(name):
                continue
            # Skip collapsed single-token names (valid "First Last" must keep a space).
            if " " not in repaired and " " in name:
                continue
            conn.execute(
                "UPDATE auction_prices_full SET player_name = ? WHERE rowid = ?",
                (repaired.strip(), rowid),
            )
            _write_alias_cache(
                conn,
                _normalize_key(name),
                repaired.strip(),
                None,
                "auction_name_repair",
            )
            updated += 1
            print(f"  {name!r} -> {repaired.strip()!r}")
        conn.commit()
    finally:
        conn.close()
    print(f"\nUpdated {updated} rows.")


if __name__ == "__main__":
    main()
