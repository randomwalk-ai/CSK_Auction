#!/usr/bin/env python3
"""
Seed IPL 2026 CSK squad prices into auction_prices_full + CSV backup.

Sources merged (auction hammer prices override squad-list amounts for buys):
  1. data/cricbuzz_all_bids/2026/CSK.csv — Cricbuzz hammer prices
  2. bid_history — CSK 2026 wins already in DB
  3. Official IPL 2026 CSK squad list (Sportstar / The Hindu retention/trade slabs)

Run:
  python scripts/seed_csk_2026_prices.py
  python scripts/seed_csk_2026_prices.py --import-db
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))

from csk_squad_catalog import CSK_2026_OFFICIAL_SQUAD  # noqa: E402

DB_PATH = ROOT / "auction_data.db"
OUT_CSV = ROOT / "data" / "csk_2026_official_prices.csv"
SCRAPE_CSV = ROOT / "data" / "cricbuzz_all_bids" / "2026" / "CSK.csv"
SOURCE_TAG = "official_ipl_2026_squad"


def _norm(name: str) -> str:
    return (name or "").strip().lower()


def _load_scrape_wins() -> dict[str, float]:
    out: dict[str, float] = {}
    if not SCRAPE_CSV.exists():
        return out
    with SCRAPE_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("bid_war") or "").lower() != "won":
                continue
            name = (row.get("player_name") or "").strip()
            if not name:
                continue
            try:
                price = round(float(row.get("last_bid_cr") or 0), 2)
            except (TypeError, ValueError):
                continue
            if price > 0:
                out[_norm(name)] = price
    return out


def build_price_rows() -> list[dict]:
    scrape = _load_scrape_wins()
    rows: list[dict] = []
    for player in CSK_2026_OFFICIAL_SQUAD:
        name = player["name"]
        key = _norm(name)
        acquisition = player.get("acquisition") or ("retained" if player.get("retained") else "auction")
        if key in scrape:
            price = scrape[key]
            status = "auction_won"
            note = "Cricbuzz 2026 hammer"
        else:
            price = round(float(player.get("price") or 0), 2)
            status = acquisition
            note = "Sportstar/Hindu IPL 2026 squad list"
        if price <= 0:
            continue
        rows.append(
            {
                "player_name": name,
                "year": 2026,
                "role": player.get("role") or "",
                "country": player.get("country") or "India",
                "price": price,
                "status": status,
                "team": "Chennai Super Kings",
                "team_code": "CSK",
                "source": SOURCE_TAG,
                "note": note,
            }
        )
    return rows


def write_csv(rows: list[dict]) -> None:
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "player_name",
        "year",
        "role",
        "country",
        "price",
        "status",
        "team_code",
        "source",
        "note",
    ]
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})
    print(f"Wrote {len(rows)} rows → {OUT_CSV}")


def import_to_db(rows: list[dict], db_path: Path) -> None:
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
        "DELETE FROM auction_prices_full WHERE team_code = 'CSK' AND year = 2026 AND source = ?",
        (SOURCE_TAG,),
    )
    for row in rows:
        conn.execute(
            """
            INSERT INTO auction_prices_full
            (player_name, year, role, country, price, status, team, team_code, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["player_name"],
                row["year"],
                row["role"],
                row["country"],
                row["price"],
                row["status"],
                row["team"],
                row["team_code"],
                row["source"],
            ),
        )
    conn.commit()
    count = conn.execute(
        "SELECT COUNT(*) FROM auction_prices_full WHERE team_code='CSK' AND year=2026 AND source=?",
        (SOURCE_TAG,),
    ).fetchone()[0]
    conn.close()
    print(f"Imported {count} CSK 2026 prices into {db_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed CSK 2026 squad prices")
    parser.add_argument("--import-db", action="store_true", help="Write to auction_data.db")
    parser.add_argument("--db", type=str, default=str(DB_PATH))
    args = parser.parse_args()

    rows = build_price_rows()
    write_csv(rows)
    if args.import_db:
        import_to_db(rows, Path(args.db))
    else:
        print("Dry run — pass --import-db to write auction_prices_full")


if __name__ == "__main__":
    main()
