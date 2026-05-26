"""
scripts/seed_missing_cricbuzz_ids.py
=====================================
Fixes missing/wrong cricbuzz_player_id in auction_prices_full (year=2026)
by cross-referencing the auction CSV which has IDs scraped directly from
Cricbuzz auction pages — those are authoritative.

Only falls back to manual overrides for players genuinely absent from the CSV.

Run from project root:
    python3 scripts/seed_missing_cricbuzz_ids.py           # dry-run, show diff
    python3 scripts/seed_missing_cricbuzz_ids.py --apply   # write to DB
    python3 scripts/seed_missing_cricbuzz_ids.py --apply --fetch  # write + re-fetch portraits
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB   = ROOT / "auction_data.db"

# Primary source — Cricbuzz-scraped, auction-linked IDs
CSV_PATH = ROOT / "data" / "cricbuzz_auction_all_teams.csv"

# DB name → CSV norm key when PDF scrape split the name (same player).
CSV_NAME_ALIASES: dict[str, str] = {
    "jamie overton": "ja mie overton",
}

# Only when absent from CSV even after aliases. Verify: /profiles/<id>
MANUAL_OVERRIDES: dict[str, int] = {
    "Jamie Overton": 8512,  # https://www.cricbuzz.com/profiles/8512/jamie-overton
}


# ── Name normalisation ────────────────────────────────────────────────────────

def _norm(name: str) -> str:
    if not name:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(name))
    ascii_ = nfkd.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_.lower()).strip()


# ── Load CSV ID map ───────────────────────────────────────────────────────────

def load_csv_ids() -> dict[str, tuple[str, int]]:
    """
    Returns { norm_name: (canonical_name, cricbuzz_player_id) }
    Most recent year's entry per player wins.
    """
    if not CSV_PATH.exists():
        sys.exit(f"❌  CSV not found: {CSV_PATH}")

    id_map: dict[str, tuple[str, int, int]] = {}

    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []

        name_col = next((h for h in headers if "player" in h.lower() and "name" in h.lower()), None)
        id_col   = next((h for h in headers if "cricbuzz" in h.lower() and "id" in h.lower()), None)
        year_col = next((h for h in headers if "year" in h.lower()), None)

        if not name_col or not id_col:
            print(f"CSV columns: {headers}")
            sys.exit("❌  Could not find player_name or cricbuzz_player_id column.")

        for row in reader:
            name    = (row.get(name_col) or "").strip()
            raw_id  = (row.get(id_col) or "").strip()
            raw_yr  = (row.get(year_col) or "0").strip()

            if not name or not raw_id or not raw_id.isdigit():
                continue

            pid  = int(raw_id)
            year = int(raw_yr) if raw_yr.isdigit() else 0
            key  = _norm(name)

            existing = id_map.get(key)
            if existing is None or year > existing[2]:
                id_map[key] = (name, pid, year)

    print(f"CSV loaded: {len(id_map)} unique player IDs")
    return {k: (v[0], v[1]) for k, v in id_map.items()}


# ── Load 2026 pool from DB ────────────────────────────────────────────────────

def load_pool(conn: sqlite3.Connection) -> list[tuple[str, str | None]]:
    """One row per player — best non-empty ID if any duplicate rows exist."""
    rows = conn.execute("""
        SELECT player_name,
               MAX(NULLIF(TRIM(cricbuzz_player_id), '')) AS cricbuzz_player_id
        FROM auction_prices_full
        WHERE year = 2026
          AND player_name IS NOT NULL
          AND TRIM(player_name) != ''
        GROUP BY player_name
        ORDER BY player_name
    """).fetchall()
    return [(r[0], r[1]) for r in rows]


def _csv_lookup(norm: str, csv_ids: dict[str, tuple[str, int]]) -> tuple[str, int] | None:
    key = CSV_NAME_ALIASES.get(norm, norm)
    if key in csv_ids:
        return csv_ids[key]
    return None


# ── Build diff ────────────────────────────────────────────────────────────────

def build_updates(
    pool: list[tuple[str, str | None]],
    csv_ids: dict[str, tuple[str, int]],
) -> list[dict]:
    updates = []
    for db_name, db_id in pool:
        norm = _norm(db_name)

        hit = _csv_lookup(norm, csv_ids)
        if hit:
            _, new_id = hit
            source = "csv"
        elif db_name in MANUAL_OVERRIDES:
            new_id = MANUAL_OVERRIDES[db_name]
            source = "manual"
        else:
            continue

        if str(db_id or "").strip() == str(new_id):
            continue

        updates.append({"name": db_name, "old_id": db_id, "new_id": new_id, "source": source})

    return updates


# ── Report unresolved ─────────────────────────────────────────────────────────

def report_unresolved(
    pool: list[tuple[str, str | None]],
    csv_ids: dict[str, tuple[str, int]],
    updates: list[dict],
) -> None:
    updated_names = {u["name"] for u in updates}
    unresolved = []
    for db_name, db_id in pool:
        norm   = _norm(db_name)
        has_id = str(db_id or "").strip() not in ("", "None")
        if (
            not has_id
            and _csv_lookup(norm, csv_ids) is None
            and db_name not in MANUAL_OVERRIDES
            and db_name not in updated_names
        ):
            unresolved.append(db_name)

    if unresolved:
        print(f"\n⚠  {len(unresolved)} players still have no ID anywhere:")
        for name in unresolved:
            print(f"   {name}")
        print("\n   Look up on https://www.cricbuzz.com and add to MANUAL_OVERRIDES.")


# ── Apply ─────────────────────────────────────────────────────────────────────

def apply_updates(conn: sqlite3.Connection, updates: list[dict]) -> None:
    for u in updates:
        conn.execute("""
            UPDATE auction_prices_full
            SET cricbuzz_player_id = ?
            WHERE player_name = ? AND year = 2026
        """, (str(u["new_id"]), u["name"]))
    conn.commit()
    print(f"✓ Updated {len(updates)} rows in auction_prices_full")


# ── Portrait re-fetch ─────────────────────────────────────────────────────────

def refetch_portraits(names: list[str], *, delay_s: float = 1.5) -> None:
    sys.path.insert(0, str(ROOT / "api"))
    try:
        from player_portrait_store import (  # noqa: E402
            PHOTO_SOURCES,
            _clear_portrait_entry,
            _connect,
            _normalize_key,
            _rebuild_equivalent_keys,
            fetch_and_cache_portrait,
            portrait_image_bytes,
        )
    except ImportError as e:
        print(f"\n⚠  Cannot import portrait store: {e}")
        print("   Run: python3 scripts/player_portraits.py update --force-initials --delay 1.5")
        return

    conn = _connect()
    _rebuild_equivalent_keys(conn)
    portrait_image_bytes.cache_clear()
    ok = initials = failed = 0

    print(f"\n⬇  Re-fetching portraits for {len(names)} players …\n")
    try:
        import time

        for name in sorted(names):
            try:
                _clear_portrait_entry(conn, _normalize_key(name))
            except Exception:
                pass
            try:
                _data, _ct, source = fetch_and_cache_portrait(name, conn=conn)
                if source in PHOTO_SOURCES:
                    print(f"  ✓ {name:<30}  [{source}]")
                    ok += 1
                elif source == "initials":
                    print(f"  🔤 {name:<30}  [initials]")
                    initials += 1
                else:
                    print(f"  ✗ {name:<30}  [{source}]")
                    failed += 1
            except Exception as exc:
                print(f"  ✗ {name:<30}  error: {exc}")
                failed += 1
            if delay_s > 0:
                time.sleep(delay_s)
    finally:
        conn.close()
        portrait_image_bytes.cache_clear()

    print(f"\nResult: {ok} real photos | {initials} initials | {failed} failed")


# ── Coverage ──────────────────────────────────────────────────────────────────

def coverage(label: str) -> None:
    import json
    mp = ROOT / "data" / "player_portraits" / "manifest.json"
    if not mp.exists():
        return
    payload = json.loads(mp.read_text(encoding="utf-8"))
    players = payload.get("players") if isinstance(payload.get("players"), dict) else payload
    if not isinstance(players, dict):
        return
    real = sum(
        1 for v in players.values()
        if str(v.get("source", "")) in ("cricbuzz", "thesportsdb", "wikipedia")
        and not str(v.get("file", "")).endswith(".svg")
    )
    total = len(players)
    print(f"[{label}] Portrait coverage: {real}/{total} real photos ({round(100*real/total)}%)")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Write corrected IDs to DB")
    parser.add_argument("--fetch", action="store_true", help="Re-fetch portraits (requires --apply)")
    parser.add_argument("--delay", type=float, default=1.5, help="Seconds between portrait fetches")
    args = parser.parse_args()

    if not DB.exists():
        sys.exit(f"❌  DB not found: {DB}")

    conn    = sqlite3.connect(DB)
    csv_ids = load_csv_ids()
    pool    = load_pool(conn)

    print(f"DB pool   : {len(pool)} players (2026)")

    updates = build_updates(pool, csv_ids)

    dry = not args.apply
    header = "DRY RUN — " if dry else ""
    print(f"\n{header}{len(updates)} ID corrections:\n")

    for u in updates:
        src = "📋 csv" if u["source"] == "csv" else "✍️  manual"
        print(f"  {u['name']:<30}  {str(u['old_id'] or 'NULL'):>10} → {u['new_id']}  {src}")

    report_unresolved(pool, csv_ids, updates)

    if not updates:
        print("\n✓ All IDs already correct.")
        conn.close()
        return

    if args.apply:
        coverage("before")
        apply_updates(conn, updates)
        if args.fetch:
            refetch_portraits([u["name"] for u in updates], delay_s=args.delay)
        coverage("after ")
    else:
        print(f"\nRun with --apply to write to DB.")
        print(f"Run with --apply --fetch to also re-fetch portraits.")

    conn.close()


if __name__ == "__main__":
    main()