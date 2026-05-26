"""
Bridge between SQLite database and Valuation Engine
NO external imports - self-contained
"""
import sqlite3
import pandas as pd
from dataclasses import dataclass, field
from typing import Dict, List
from collections import defaultdict

# Define PlayerStats here - no import needed
@dataclass
class PlayerStats:
    """Raw aggregated stats per player across all matches."""
    name: str
    # Batting
    bat_innings: int = 0
    bat_runs: int = 0
    bat_balls: int = 0
    bat_fours: int = 0
    bat_sixes: int = 0
    bat_dismissals: int = 0
    # Phase batting
    pp_bat_runs: int = 0
    pp_bat_balls: int = 0
    mid_bat_runs: int = 0
    mid_bat_balls: int = 0
    death_bat_runs: int = 0
    death_bat_balls: int = 0
    # Bowling
    bowl_balls: int = 0
    bowl_runs: int = 0
    bowl_wickets: int = 0
    bowl_dots: int = 0
    bowl_innings: int = 0
    # Phase bowling
    pp_bowl_balls: int = 0
    pp_bowl_runs: int = 0
    pp_bowl_wkts: int = 0
    mid_bowl_balls: int = 0
    mid_bowl_runs: int = 0
    mid_bowl_wkts: int = 0
    death_bowl_balls: int = 0
    death_bowl_runs: int = 0
    death_bowl_wkts: int = 0
    # Match logs
    match_log: List[Dict] = field(default_factory=list)
    recent_bat_sr: float = 0.0
    recent_bowl_econ: float = 0.0
    total_matches: int = 0


def load_stats_from_db(db_path="auction_data.db"):
    """Load player stats from SQLite into PlayerStats format"""
    
    conn = sqlite3.connect(db_path)
    
    # Fetch batting stats
    batting_df = pd.read_sql("""
        SELECT player_name, total_runs, strike_rate, average, 
               matches_played, highest_score, fifties, hundreds
        FROM player_auction_stats
        WHERE total_runs > 0
    """, conn)
    
    # Fetch bowling stats  
    bowling_df = pd.read_sql("""
        SELECT player_name, total_wickets, economy_rate, bowling_average
        FROM player_auction_stats
        WHERE total_wickets > 0
    """, conn)
    
    conn.close()
    
    # Convert to PlayerStats format
    stats_db = {}
    
    for _, row in batting_df.iterrows():
        ps = PlayerStats(name=row['player_name'])
        ps.bat_runs = int(row['total_runs'])
        ps.bat_dismissals = int(row['total_runs'] / row['average']) if row['average'] and row['average'] > 0 else 0
        ps.total_matches = int(row['matches_played']) if row['matches_played'] else 0
        stats_db[row['player_name']] = ps
    
    for _, row in bowling_df.iterrows():
        if row['player_name'] in stats_db:
            ps = stats_db[row['player_name']]
        else:
            ps = PlayerStats(name=row['player_name'])
            stats_db[row['player_name']] = ps
        ps.bowl_wickets = int(row['total_wickets']) if row['total_wickets'] else 0
        ps.bowl_runs = int(row['total_wickets'] * row['bowling_average']) if row['bowling_average'] and row['bowling_average'] > 0 else 0
    
    return stats_db


if __name__ == "__main__":
    stats = load_stats_from_db()
    print(f"Loaded {len(stats)} players for valuation")