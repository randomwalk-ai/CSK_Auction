"""
Load player stats from SQLite, preferring IPL competition rows.
"""

from __future__ import annotations

import re
import sqlite3
from difflib import get_close_matches
from typing import Dict, List, Optional

# Full names (dashboard / IPL squad) → Cricinfo-style rows in player_auction_stats
PLAYER_NAME_ALIASES: Dict[str, str] = {
    "sanju samson": "SV Samson",
    "ruturaj gaikwad": "RD Gaikwad",
    "shivam dube": "S Dube",
    "rahul chahar": "RD Chahar",
    "deepak chahar": "DL Chahar",
    "dewald brevis": "D Brevis",
    "prashant veer": "PR Veer",
    "anshul kamboj": "A Kamboj",
    "khaleel ahmed": "I Khaleel",
    "mukesh choudhary": "M Choudhary",
    "gurjapneet singh": "Gurjapneet Singh",
    "noor ahmad": "Noor Ahmad",
    "nathan ellis": "NT Ellis",
    "mukul choudhary": "M Choudhary",
    "ayush mhatre": "Ayush Mhatre",
    "urvil patel": "Urvil Patel",
    "ramakrishna ghosh": "AN Ghosh",
    "jamie overton": "Jamie Overton",
    "matthew short": "DJM Short",
    "matt henry": "MJ Henry",
    "akeal hosein": "AJ Hosein",
    "shreyas gopal": "Shreyas Gopal",
    "zachary foulkes": "ZGF Foulkes",
    "zakary foulkes": "ZGF Foulkes",
    "prithvi shaw": "PP Shaw",
    "sahil parakh": "SU Parakh",
    "danish malewar": "DV Malewar",
    "josh hazlewood": "JR Hazlewood",
}

# IPL stats for auction/squad names missing from ball-by-ball ingest (Cricinfo abbrev or no JSON coverage).
SEEDED_PLAYER_STATS: Dict[str, Dict] = {
    "sarfaraz khan": {
        "player_id": "Sarfaraz Khan",
        "player_name": "Sarfaraz Khan",
        "competition": "ipl",
        "matches_played": 50,
        "total_runs": 585,
        "average": 22.5,
        "strike_rate": 130.58,
        "highest_score": 67,
        "fifties": 1,
        "hundreds": 0,
        "total_wickets": 0,
        "economy_rate": None,
        "last_10_matches_runs": 118,
        "last_10_matches_avg": 19.7,
        "last_10_matches_sr": 128.0,
        "last_10_matches_wickets": 0,
        "last_10_matches_economy": None,
        "form_rating": 36.0,
    },
}


def normalize_player_name(name: str) -> str:
    name = name.strip().lower()
    name = re.sub(r"\b[a-z]\.\s*", "", name)
    name = re.sub(r"\s+", " ", name)
    return name.title()


def _first_initial_token(first: str) -> str:
    return (first or "").strip().lower()[:1]


def _initials_compatible(search_first: str, db_first: str) -> bool:
    """Match 'Sanju' ↔ 'SV', 'Ruturaj' ↔ 'RD', 'MS' ↔ 'MS'."""
    sf = search_first.strip().lower()
    df = db_first.strip().lower()
    if not sf or not df:
        return False
    if sf == df:
        return True
    if sf[0] != df[0]:
        return False
    if len(df) <= 3:
        return all(i < len(sf) and df[i] == sf[i] for i in range(len(df)))
    return sf.startswith(df) or df.startswith(sf[: len(df)])


def _match_by_surname_and_initial(all_names: List[str], normalized: str) -> Optional[str]:
    parts = normalized.split()
    if len(parts) < 2:
        return None

    search_last = parts[-1].lower()
    search_first = parts[0]

    candidates: List[str] = []
    for db_name in all_names:
        db_parts = db_name.split()
        if len(db_parts) < 2:
            continue
        if db_parts[-1].lower() != search_last:
            continue
        if _initials_compatible(search_first, db_parts[0]):
            candidates.append(db_name)

    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        # Prefer IPL row names that are shorter abbreviations (Cricinfo style)
        candidates.sort(key=lambda n: (len(n.split()[0]), n))
        return candidates[0]
    return None


def find_player_by_fuzzy_name(conn: sqlite3.Connection, search_name: str) -> Optional[str]:
    normalized = normalize_player_name(search_name)
    key = normalized.lower()

    alias = PLAYER_NAME_ALIASES.get(key)
    if alias:
        row = conn.execute(
            "SELECT player_name FROM player_auction_stats WHERE player_name = ? LIMIT 1",
            (alias,),
        ).fetchone()
        if row:
            return row[0]

    row = conn.execute(
        """
        SELECT player_name FROM player_auction_stats
        WHERE LOWER(player_name) = ?
        LIMIT 1
        """,
        (normalized.lower(),),
    ).fetchone()
    if row:
        return row[0]

    all_names = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT player_name FROM player_auction_stats"
        ).fetchall()
    ]

    by_initial = _match_by_surname_and_initial(all_names, normalized)
    if by_initial:
        return by_initial

    matches = get_close_matches(normalized, all_names, n=5, cutoff=0.88)
    if matches:
        search_last = normalized.split()[-1].lower()
        search_first = normalized.split()[0]
        for candidate in matches:
            cand_parts = candidate.split()
            if cand_parts[-1].lower() != search_last:
                continue
            if _initials_compatible(search_first, cand_parts[0]):
                return candidate
    return None


def find_player_via_auction_roster(
    conn: sqlite3.Connection,
    search_name: str,
    *,
    years: tuple = (2025, 2026),
) -> Optional[str]:
    """
    Link auction roster full names (e.g. Josh Hazlewood) to stats rows (e.g. JR Hazlewood).
    Used when the player is in auction_prices_full but fuzzy stats match failed.
    """
    clean = (search_name or "").strip()
    if not clean:
        return None

    norm = normalize_player_name(clean)
    on_roster = False
    for year in years:
        row = conn.execute(
            """
            SELECT 1 FROM auction_prices_full
            WHERE year = ?
              AND (
                    LOWER(TRIM(player_name)) = LOWER(?)
                 OR LOWER(TRIM(player_name)) = LOWER(?)
              )
            LIMIT 1
            """,
            (year, clean, norm),
        ).fetchone()
        if row:
            on_roster = True
            break

    if not on_roster:
        return None

    parts = norm.split()
    if len(parts) < 2:
        return None

    search_last = parts[-1].lower()
    search_first = parts[0]

    ipl_names = [
        r[0]
        for r in conn.execute(
            """
            SELECT DISTINCT player_name FROM player_auction_stats
            WHERE LOWER(competition) = 'ipl'
            """
        ).fetchall()
    ]
    all_names = ipl_names or [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT player_name FROM player_auction_stats"
        ).fetchall()
    ]

    candidates = [
        n for n in all_names
        if len(n.split()) >= 2 and n.split()[-1].lower() == search_last
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    for cand in candidates:
        if _initials_compatible(search_first, cand.split()[0]):
            return cand

    matches = get_close_matches(norm, candidates, n=3, cutoff=0.82)
    if matches:
        return matches[0]

    candidates.sort(key=lambda n: (len(n.split()[0]), n))
    return candidates[0]


def _row_to_dict(row: sqlite3.Row) -> Dict:
    return {k: row[k] for k in row.keys()}


def get_seeded_player_stats(name: str) -> Optional[Dict]:
    key = normalize_player_name(name).lower()
    row = SEEDED_PLAYER_STATS.get(key)
    return dict(row) if row else None


def get_player_stats(conn: sqlite3.Connection, name: str) -> Optional[Dict]:
    """
    Return best stats row for a player.
    Prefers competition='ipl', then highest matches_played.
    Falls back to curated seeds when ball-by-ball data is missing.
    """
    matched = find_player_by_fuzzy_name(conn, name)
    if not matched:
        matched = find_player_via_auction_roster(conn, name)
    if matched:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM player_auction_stats
            WHERE player_name = ?
            ORDER BY
                CASE WHEN LOWER(competition) = 'ipl' THEN 0 ELSE 1 END,
                matches_played DESC
            """,
            (matched,),
        ).fetchall()

        if rows:
            return _row_to_dict(rows[0])

    seeded = get_seeded_player_stats(name)
    if seeded:
        return seeded
    return None
