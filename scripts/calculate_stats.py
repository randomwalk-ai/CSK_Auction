import sqlite3
import pandas as pd
from datetime import datetime, timedelta
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class AuctionStatsCalculator:
    def __init__(self, db_path="auction_data.db"):
        self.conn = sqlite3.connect(db_path)
    
    def calculate_batting_stats(self):
        """Calculate comprehensive batting statistics - COMPLETELY REWRITTEN"""
        
        # FIXED BUGS 2 & 3: Proper strike rate (exclude wides) and average (aggregate per innings)
        query = """
        WITH innings_totals AS (
            -- Step 1: Aggregate each batter's performance per match
            SELECT 
                d.batter as player_name,
                d.match_id,
                m.competition,
                m.format_weight,
                SUM(d.runs_batter) as innings_runs,
                COUNT(CASE WHEN d.is_wide = 0 AND d.is_noball = 0 THEN 1 END) as balls_faced,
                MAX(CASE WHEN d.wicket_player = d.batter THEN 1 ELSE 0 END) as was_dismissed
            FROM deliveries d
            JOIN matches m ON d.match_id = m.match_id
            WHERE d.batter IS NOT NULL
            GROUP BY d.batter, d.match_id, m.competition, m.format_weight
        ),
        player_stats AS (
            -- Step 2: Aggregate across all matches with format weighting
            SELECT 
                player_name,
                competition,
                COUNT(*) as matches_played,
                SUM(innings_runs) as total_runs,
                -- FIXED BUG 3: Average = total runs / dismissals
                ROUND(SUM(innings_runs) * 1.0 / NULLIF(SUM(was_dismissed), 0), 2) as average,
                -- FIXED BUG 2: Strike rate = (runs * 100) / balls faced (excluding wides)
                ROUND(SUM(innings_runs) * 100.0 / NULLIF(SUM(balls_faced), 0), 2) as strike_rate,
                MAX(innings_runs) as highest_score,
                SUM(CASE WHEN innings_runs >= 50 AND innings_runs < 100 THEN 1 ELSE 0 END) as fifties,
                SUM(CASE WHEN innings_runs >= 100 THEN 1 ELSE 0 END) as hundreds,
                -- Weighted stats using format weights
                SUM(innings_runs * format_weight) as weighted_runs,
                SUM(balls_faced * format_weight) as weighted_balls
            FROM innings_totals
            GROUP BY player_name, competition
        )
        INSERT OR REPLACE INTO player_auction_stats
        (player_id, player_name, competition, matches_played, total_runs, average, 
         strike_rate, highest_score, fifties, hundreds)
        SELECT 
            p.identifier,
            ps.player_name,
            ps.competition,
            ps.matches_played,
            ps.total_runs,
            ps.average,
            ps.strike_rate,
            ps.highest_score,
            ps.fifties,
            ps.hundreds
        FROM player_stats ps
        LEFT JOIN players p ON p.name = ps.player_name
        """
        self.conn.execute(query)
        self.conn.commit()
        logger.info("Batting statistics calculated")
    
    def calculate_bowling_stats(self):
            """Calculate bowling statistics - FIXED"""
            query = """
            WITH bowling_innings AS (
                -- Aggregate per bowler per match
                SELECT 
                    d.bowler as player_name,
                    d.match_id,
                    m.competition,
                    m.format_weight,
                    COUNT(CASE WHEN d.is_wide = 0 AND d.is_noball = 0 THEN 1 END) as legal_balls,
                    SUM(d.runs_total) as runs_conceded,
                    COUNT(CASE WHEN d.wicket_type IS NOT NULL THEN 1 END) as wickets
                FROM deliveries d
                JOIN matches m ON d.match_id = m.match_id
                WHERE d.bowler IS NOT NULL
                GROUP BY d.bowler, d.match_id, m.competition, m.format_weight
            ),
            bowler_stats AS (
                SELECT 
                    player_name,
                    competition,
                    COUNT(*) as matches_played,
                    SUM(wickets) as total_wickets,
                    SUM(runs_conceded) as total_runs,
                    SUM(legal_balls) as total_balls,
                    -- FIXED: Economy = (runs * 6) / overs (6 balls per over)
                    ROUND((SUM(runs_conceded) * 6.0) / NULLIF(SUM(legal_balls), 0), 2) as economy_rate,
                    -- Bowling average = runs conceded / wickets
                    ROUND(SUM(runs_conceded) * 1.0 / NULLIF(SUM(wickets), 0), 2) as bowling_average,
                    -- Strike rate = balls / wickets
                    ROUND(SUM(legal_balls) * 1.0 / NULLIF(SUM(wickets), 0), 1) as bowling_sr
                FROM bowling_innings
                GROUP BY player_name, competition
            )
            UPDATE player_auction_stats 
            SET total_wickets = bs.total_wickets,
                bowling_average = bs.bowling_average,
                economy_rate = bs.economy_rate
            FROM bowler_stats bs
            WHERE player_auction_stats.player_name = bs.player_name
            AND player_auction_stats.competition = bs.competition
            """
            self.conn.execute(query)
            self.conn.commit()
            logger.info("Bowling statistics calculated")
        
    def calculate_recent_form(self):
        """Calculate last 10 matches performance - FULLY FIXED"""
        
        query = """
        WITH match_performances AS (
            SELECT 
                d.batter as player_name,
                d.match_id,
                m.competition,
                m.date,
                m.format_weight,
                SUM(d.runs_batter) as match_runs,
                COUNT(CASE WHEN d.is_wide = 0 AND d.is_noball = 0 THEN 1 END) as match_balls,
                CASE WHEN MAX(CASE WHEN d.wicket_player = d.batter THEN 1 ELSE 0 END) = 1 
                    THEN SUM(d.runs_batter) ELSE NULL END as runs_if_dismissed
            FROM deliveries d
            JOIN matches m ON d.match_id = m.match_id
            WHERE d.batter IS NOT NULL
            GROUP BY d.batter, d.match_id, m.competition, m.date, m.format_weight
        ),
        bowling_matches AS (
            SELECT 
                d.bowler as player_name,
                d.match_id,
                m.competition,
                m.date,
                m.format_weight,
                COUNT(CASE WHEN d.wicket_type IS NOT NULL THEN 1 END) as match_wickets,
                SUM(CASE WHEN d.is_wide = 0 AND d.is_noball = 0 THEN d.runs_total ELSE 0 END) as match_runs,
                COUNT(CASE WHEN d.is_wide = 0 AND d.is_noball = 0 THEN 1 END) as match_balls
            FROM deliveries d
            JOIN matches m ON d.match_id = m.match_id
            WHERE d.bowler IS NOT NULL
            GROUP BY d.bowler, d.match_id, m.competition, m.date, m.format_weight
        ),
        all_players_list AS (
            SELECT DISTINCT player_name FROM match_performances
            UNION
            SELECT DISTINCT player_name FROM bowling_matches
        ),
        combined_matches AS (
            SELECT 
                apl.player_name,
                COALESCE(bp.match_id, bw.match_id) as match_id,
                COALESCE(bp.date, bw.date) as match_date,
                COALESCE(bp.format_weight, bw.format_weight, 1.0) as format_weight,
                COALESCE(bp.match_runs, 0) as runs,
                COALESCE(bp.match_balls, 0) as balls_faced,
                COALESCE(bw.match_wickets, 0) as wickets,
                COALESCE(bw.match_runs, 0) as bowl_runs,
                COALESCE(bw.match_balls, 0) as balls_bowled
            FROM all_players_list apl
            LEFT JOIN match_performances bp ON apl.player_name = bp.player_name
            LEFT JOIN bowling_matches bw ON apl.player_name = bw.player_name AND bp.match_id = bw.match_id
        ),
        ranked_matches AS (
            SELECT 
                player_name,
                match_id,
                match_date,
                format_weight,
                runs,
                wickets,
                balls_faced,
                bowl_runs,
                balls_bowled,
                CASE WHEN balls_faced > 0 THEN (runs * 100.0 / balls_faced) ELSE 0 END as match_sr,
                CASE WHEN balls_bowled > 0 THEN (bowl_runs * 6.0 / balls_bowled) ELSE 0 END as match_econ,
                ROW_NUMBER() OVER (PARTITION BY player_name ORDER BY match_date DESC) as match_recency
            FROM combined_matches
            WHERE match_date IS NOT NULL
        ),
        form_stats AS (
            SELECT 
                player_name,
                SUM(runs) as last_10_runs,
                SUM(wickets) as last_10_wickets,
                SUM(runs * format_weight) as weighted_runs,
                SUM(wickets * format_weight) as weighted_wickets,
                SUM(balls_faced) as total_balls_faced,
                SUM(balls_bowled) as total_balls_bowled,
                COUNT(*) as matches_in_sample,
                ROUND(AVG(CASE WHEN runs > 0 THEN runs END), 2) as last_10_avg,
                ROUND(
                    SUM(CASE WHEN balls_faced > 0 THEN (runs * 100.0 / balls_faced) * format_weight ELSE 0 END)
                    / NULLIF(SUM(CASE WHEN balls_faced > 0 THEN format_weight ELSE 0 END), 0),
                2) as last_10_sr,
                ROUND(
                    SUM(CASE WHEN balls_bowled > 0 THEN (bowl_runs * 6.0 / balls_bowled) * format_weight ELSE 0 END)
                    / NULLIF(SUM(CASE WHEN balls_bowled > 0 THEN format_weight ELSE 0 END), 0),
                2) as last_10_econ
            FROM ranked_matches
            WHERE match_recency <= 10 AND format_weight > 0
            GROUP BY player_name
        )
        UPDATE player_auction_stats 
        SET last_10_matches_runs = fs.last_10_runs,
            last_10_matches_avg = fs.last_10_avg,
            last_10_matches_sr = fs.last_10_sr,
            last_10_matches_wickets = fs.last_10_wickets,
            last_10_matches_economy = fs.last_10_econ,
            form_rating = ROUND(
                -- SR score (max 40 points) — format-weighted last 10
                (CASE WHEN fs.last_10_sr >= 180 THEN 40
                    WHEN fs.last_10_sr >= 150 THEN 35
                    WHEN fs.last_10_sr >= 130 THEN 28
                    WHEN fs.last_10_sr >= 110 THEN 20
                    ELSE 10 END) +
                -- Economy score (max 30 points) — format-weighted
                (CASE WHEN fs.last_10_econ <= 6 THEN 30
                    WHEN fs.last_10_econ <= 7 THEN 25
                    WHEN fs.last_10_econ <= 8 THEN 18
                    WHEN fs.last_10_econ <= 9 THEN 10
                    ELSE 5 END) +
                -- Wickets score (max 20 points) — IPL-weighted wicket sum
                (CASE WHEN fs.weighted_wickets >= 12 THEN 20
                    WHEN fs.weighted_wickets >= 8 THEN 15
                    WHEN fs.weighted_wickets >= 5 THEN 10
                    WHEN fs.weighted_wickets >= 3 THEN 5
                    ELSE 0 END) +
                -- Runs score (max 10 points) — IPL-weighted run sum
                (CASE WHEN fs.weighted_runs >= 400 THEN 10
                    WHEN fs.weighted_runs >= 300 THEN 8
                    WHEN fs.weighted_runs >= 200 THEN 5
                    WHEN fs.weighted_runs >= 100 THEN 3
                    ELSE 0 END), 
                2
            )
        FROM form_stats fs
        WHERE player_auction_stats.player_name = fs.player_name
        """
        
        self.conn.execute(query)
        self.conn.commit()
        logger.info("Recent form statistics calculated - FULLY FIXED")
        
    def run_all(self):
        """Execute all statistics calculations"""
        logger.info("Starting auction statistics calculation...")
        self.calculate_batting_stats()
        self.calculate_bowling_stats()
        self.calculate_recent_form()
        logger.info("All statistics calculated!")

if __name__ == "__main__":
    calculator = AuctionStatsCalculator()
    calculator.run_all()
