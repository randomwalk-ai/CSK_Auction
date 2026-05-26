-- Players table (fixed for actual Cricsheet people.csv)
CREATE TABLE IF NOT EXISTS players (
    identifier VARCHAR(50) PRIMARY KEY,  -- Changed from cricsheet_id
    name VARCHAR(100) NOT NULL,
    unique_name VARCHAR(100),
    country VARCHAR(50) DEFAULT 'Unknown'
);

-- Matches table with format weights
CREATE TABLE IF NOT EXISTS matches (
    match_id VARCHAR(50) PRIMARY KEY,
    competition VARCHAR(50),
    format_weight DECIMAL(4,2) DEFAULT 1.0,  -- IPL=1.0, T20I=0.9, Domestic=0.7
    season VARCHAR(10),
    date DATE,
    team1 VARCHAR(100),
    team2 VARCHAR(100),
    venue VARCHAR(200),
    winner VARCHAR(100),
    player_of_match VARCHAR(100)
);

-- Fixed deliveries table with phase tracking
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id VARCHAR(50),
    innings INTEGER,
    over_number INTEGER,        -- Now stored, not just looped
    ball INTEGER,
    batter VARCHAR(100),
    bowler VARCHAR(100),
    non_striker VARCHAR(100),
    runs_batter INTEGER,
    runs_extras INTEGER,
    runs_total INTEGER,
    is_wide BOOLEAN DEFAULT 0,   -- Critical for SR calculation
    is_noball BOOLEAN DEFAULT 0,
    wicket_type VARCHAR(50),
    wicket_player VARCHAR(100),
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
);

-- Pre-calculated player statistics for auction
CREATE TABLE IF NOT EXISTS player_auction_stats (
    player_id VARCHAR(50),
    player_name VARCHAR(100),
    competition VARCHAR(50),
    matches_played INTEGER,
    total_runs INTEGER,
    average DECIMAL(8,2),
    strike_rate DECIMAL(8,2),
    highest_score INTEGER,
    fifties INTEGER,
    hundreds INTEGER,
    total_wickets INTEGER,
    bowling_average DECIMAL(8,2),
    economy_rate DECIMAL(8,2),
    best_bowling VARCHAR(20),
    last_10_matches_runs INTEGER,
    last_10_matches_avg DECIMAL(8,2),
    last_10_matches_sr DECIMAL(8,2),
    last_10_matches_wickets INTEGER,
    last_10_matches_economy DECIMAL(8,2),
    form_rating DECIMAL(5,2),
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (player_id, competition)
);

CREATE INDEX idx_player_name ON player_auction_stats(player_name);
CREATE INDEX idx_form_rating ON player_auction_stats(form_rating DESC);