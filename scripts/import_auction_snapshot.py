#!/usr/bin/env python3
"""Import IPL auction player rows from a browser/CDP JSON snapshot into DB + CSV."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from scrape_cricbuzz_auction import convert_to_cr, parse_player_line, save_csv  # noqa: E402

DB_PATH = ROOT / "auction_data.db"
OUT_CSV = ROOT / "data" / "cricbuzz_auction_all_teams.csv"


def _parse_catalog_line(text: str, href: str, year: int) -> dict | None:
    """Players tab rows: 'Steven Smith Batter Australia 2.00 Cr' (no sold/unsold)."""
    row = parse_player_line(text, href, year)
    if row:
        return row
    import re

    from scrape_cricbuzz_auction import COUNTRIES, ROLES, convert_to_cr

    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 8:
        return None

    player_id = ""
    m_id = re.search(r"/auction/players/(\d+)", href)
    if m_id:
        player_id = m_id.group(1)

    role = ""
    for r in ROLES:
        if re.search(rf"\b{re.escape(r)}\b", text, re.I):
            role = r
            break

    country = ""
    for c in COUNTRIES:
        if re.search(rf"\b{re.escape(c)}\b", text, re.I):
            country = c
            break

    price_match = re.findall(r"([\d.]+\s*(?:Cr|L))", text, re.I)
    base_raw = price_match[-1] if price_match else ""
    base_cr = convert_to_cr(base_raw) if base_raw else 0.0

    name = text
    cut = len(text)
    for token in [role, country, base_raw, "Base Price"]:
        if token:
            idx = text.lower().find(str(token).lower())
            if idx > 2:
                cut = min(cut, idx)
    name = text[:cut].strip()
    if not name or len(name) < 2:
        return None

    return {
        "year": year,
        "player_name": name,
        "role": role,
        "country": country,
        "status": "listed",
        "base_price_raw": base_raw,
        "final_price_raw": "",
        "price_cr": 0.0,
        "team_code": "",
        "team": "",
        "cricbuzz_player_id": player_id,
        "source": "cricbuzz_playwright",
    }


def load_snapshot(path: Path, year: int) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "items" in payload:
        items = payload["items"]
    elif isinstance(payload, dict) and "result" in payload:
        inner = payload["result"]
        if isinstance(inner, dict) and "value" in inner:
            val = inner["value"]
            items = val["items"] if isinstance(val, dict) and "items" in val else val
        else:
            items = inner
    else:
        items = payload

    rows: list[dict] = []
    seen: set[str] = set()
    for item in items:
        href = str(item.get("href") or "")
        text = str(item.get("text") or "").strip()
        if not href or not text:
            continue
        parsed = parse_player_line(text, href, year) or _parse_catalog_line(text, href, year)
        if not parsed:
            continue
        key = parsed["player_name"].strip().lower()
        if key in seen:
            continue
        seen.add(key)
        rows.append(parsed)
    return rows


def merge_csv(year: int, new_rows: list[dict]) -> None:
    import pandas as pd

    if OUT_CSV.exists():
        df = pd.read_csv(OUT_CSV)
        df = df[df["year"] != year]
    else:
        df = pd.DataFrame()

    add = pd.DataFrame(new_rows)
    if add.empty:
        return
    combined = pd.concat([df, add], ignore_index=True)
    save_csv(combined.to_dict("records"), OUT_CSV)


def import_year_to_db(rows: list[dict], db_path: Path, year: int) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auction_prices_full (
            player_name TEXT,
            year INTEGER,
            role TEXT,
            country TEXT,
            price REAL,
            status TEXT,
            team TEXT,
            team_code TEXT,
            base_price_cr REAL,
            cricbuzz_player_id TEXT,
            source TEXT
        )
        """
    )
    conn.execute(
        "DELETE FROM auction_prices_full WHERE year = ? AND source = 'cricbuzz_playwright'",
        (year,),
    )
    for row in rows:
        conn.execute(
            """
            INSERT INTO auction_prices_full
            (player_name, year, role, country, price, status, team, team_code,
             base_price_cr, cricbuzz_player_id, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["player_name"],
                row["year"],
                row["role"],
                row["country"],
                row["price_cr"],
                row["status"],
                row["team"],
                row["team_code"],
                convert_to_cr(row.get("base_price_raw", "")),
                row.get("cricbuzz_player_id", ""),
                "cricbuzz_playwright",
            ),
        )
    conn.commit()
    conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("snapshot", type=Path, help="CDP/browser JSON snapshot")
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--import-db", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows = load_snapshot(args.snapshot, args.year)
    print(f"Parsed {len(rows)} unique players for {args.year}")

    if args.dry_run:
        from collections import Counter

        print("status", Counter(r.get("status") for r in rows))
        return

    merge_csv(args.year, rows)
    if args.import_db:
        import_year_to_db(rows, DB_PATH, args.year)
        print(f"Imported {len(rows)} rows → auction_prices_full")


if __name__ == "__main__":
    main()
