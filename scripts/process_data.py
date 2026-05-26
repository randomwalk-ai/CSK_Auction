import json
import sqlite3
from pathlib import Path
from datetime import datetime
import pandas as pd
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class CricsheetProcessor:
    def __init__(self, db_path="auction_data.db", data_dir="data"):
        self.db_path = db_path
        self.data_dir = Path(data_dir)
        self.conn = sqlite3.connect(db_path)
        
        # Format weights: IPL = most important
        self.format_weights = {
            "ipl": 1.0,
            "t20s": 0.9,      # International T20
            "smat": 0.7,      # Domestic (Syed Mushtaq Ali)
            "bbl": 0.85,      # Big Bash
            "psl": 0.85,      # PSL
            "cpl": 0.85,      # CPL
            "sa20": 0.85,     # SA20
            "tests": 0.0,     # Not relevant
            "odis": 0.0       # Not relevant
        }
        
    def init_database(self):
    
    # Drop existing tables to avoid conflicts
        self.conn.execute("DROP TABLE IF EXISTS deliveries")
        self.conn.execute("DROP TABLE IF EXISTS matches")
        self.conn.execute("DROP TABLE IF EXISTS players")
        self.conn.execute("DROP TABLE IF EXISTS player_auction_stats")
        
        # Now create fresh tables
        with open('database/models.sql', 'r') as f:
            self.conn.executescript(f.read())
        
        logger.info("Database initialized (fresh install)")
    
    def get_format_weight(self, competition):
        """Get weight for competition"""
        comp_lower = competition.lower()
        for key, weight in self.format_weights.items():
            if key in comp_lower:
                return weight
        return 0.5  # Default weight
    
    def process_match_file(self, file_path, competition):
        """Process a single match JSON file - FIXED"""
        try:
            with open(file_path, 'r') as f:
                match_data = json.load(f)
            
            match_info = match_data.get('info', {})
            match_id = file_path.stem
            format_weight = self.get_format_weight(competition)
            
            # Insert match data with format weight
            self.conn.execute("""
                INSERT OR REPLACE INTO matches 
                (match_id, competition, format_weight, season, date, team1, team2, venue, winner, player_of_match)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                match_id,
                competition,
                format_weight,
                match_info.get('season'),
                match_info.get('dates', [None])[0],
                match_info.get('teams', [None, None])[0],
                match_info.get('teams', [None, None])[1],
                match_info.get('venue'),
                match_info.get('outcome', {}).get('winner'),
                match_info.get('player_of_match', [None])[0]
            ))
            
            # Process innings
            for innings_idx, innings in enumerate(match_data.get('innings', []), 1):
                for over_data in innings.get('overs', []):
                    over_num = over_data.get('over')
                    
                    for delivery in over_data.get('deliveries', []):
                        # Extract runs
                        runs = delivery.get('runs', {})
                        runs_batter = runs.get('batter', 0)
                        runs_extras = runs.get('extras', 0)
                        runs_total = runs.get('total', 0)
                        
                        # Check for wides/no-balls
                        extras_data = delivery.get('extras', {})
                        is_wide = 1 if 'wides' in extras_data else 0
                        is_noball = 1 if 'noballs' in extras_data else 0
                        
                        # FIXED BUG 1: Use 'wickets' (plural) not 'wicket' (singular)
                        wickets = delivery.get('wickets', [])
                        wicket_type = None
                        wicket_player = None
                        
                        if wickets:
                            wicket = wickets[0]  # Usually only one wicket per delivery
                            wicket_type = wicket.get('kind')
                            wicket_player = wicket.get('player_out')
                        
                        # Insert delivery with all fields
                        self.conn.execute("""
                            INSERT INTO deliveries 
                            (match_id, innings, over_number, ball, batter, bowler, non_striker,
                             runs_batter, runs_extras, runs_total, is_wide, is_noball,
                             wicket_type, wicket_player)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            match_id, innings_idx, over_num, 
                            delivery.get('ball', 0),  # Add ball number if exists
                            delivery.get('batter'),
                            delivery.get('bowler'),
                            delivery.get('non_striker'),
                            runs_batter, runs_extras, runs_total,
                            is_wide, is_noball,
                            wicket_type, wicket_player
                        ))
            
            return True
            
        except Exception as e:
            logger.error(f"Error processing {file_path}: {e}")
            return False
    
    def process_competition(self, competition):
        """Process all matches for a competition"""
        comp_dir = self.data_dir / competition
        if not comp_dir.exists():
            logger.warning(f"Directory for {competition} not found")
            return
        
        json_files = list(comp_dir.glob("*.json"))
        logger.info(f"Processing {len(json_files)} matches for {competition}")
        
        for i, json_file in enumerate(json_files):
            if i % 100 == 0:
                logger.info(f"Processed {i}/{len(json_files)} matches for {competition}")
            self.process_match_file(json_file, competition)
        
        self.conn.commit()
        logger.info(f"Completed processing {competition}")
    
    def load_players(self):
        """Extract players directly from match JSON files - no people.csv needed""" 
        logger.info("Extracting players from match JSON files...")
    
        players_set = set()
        json_files = list(self.data_dir.glob("**/*.json")) + list(self.data_dir.glob("*.json"))
    
        for file_path in json_files:
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    info = data.get('info', {})
                
                # Extract players from match info
                    players_dict = info.get('players', {})
                    for team, team_players in players_dict.items():
                        if isinstance(team_players, list):
                            players_set.update(team_players)
                        elif isinstance(team_players, dict):
                            players_set.update(team_players.keys())
                        else:
                            players_set.add(str(team_players))
            except Exception as e:
                logger.debug(f"Error parsing {file_path}: {e}")
            continue
    
    # Insert into database
        for player_name in players_set:
            if player_name and player_name.strip():
                try:
                    self.conn.execute("""
                    INSERT OR IGNORE INTO players (identifier, name, country)
                    VALUES (?, ?, ?)
                """, (player_name[:50], player_name, 'Unknown'))
                except Exception:
                    pass
    
        self.conn.commit()
        count = self.conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        logger.info(f"Loaded {count} unique players from match files")
    
    def run_all(self):
        """Process all competitions"""
        self.init_database()
        self.load_players()
        
        competitions = ["ipl", "t20s", "smat", "bbl", "psl", "cpl", "sa20"]
        for competition in competitions:
            self.process_competition(competition)
        
        logger.info("All data processing complete!")

if __name__ == "__main__":
    processor = CricsheetProcessor()
    processor.run_all()