"""
Import auction data from Excel files into SQLite database
"""

import sqlite3
import pandas as pd
import os
import re

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "auction_data.db")
DATA_FOLDER = "/Users/rithanipriyankaasr/Desktop/CSK_2/Data/Auction"

def convert_to_cr(price_str):
    """Convert '1.00 Cr' or '20.00 L' to Crores (number)"""
    if pd.isna(price_str):
        return 0
    if price_str == '--' or price_str == '-':
        return 0
    
    price_str = str(price_str).strip()
    
    # Extract numbers using regex
    match = re.search(r'([\d.]+)', price_str)
    if not match:
        return 0
    
    value = float(match.group(1))
    
    if 'Cr' in price_str or 'cr' in price_str:
        return value
    elif 'L' in price_str or 'l' in price_str:
        return value / 100  # Lakhs to Crores
    else:
        return value  # Assume already in Crores

def import_auction_prices():
    """Import historical auction prices"""
    
    file_path = os.path.join(DATA_FOLDER, "Auction.xlsx")
    print(f"Reading: {file_path}")
    
    auction_df = pd.read_excel(file_path, sheet_name="Sheet 1", header=1)
    
    # Clean column names
    auction_df.columns = ['Year', 'Role', 'Player', 'Country', 'Price', 'Notes']
    
    # Remove rows with NaN Player
    auction_df = auction_df.dropna(subset=['Player'])
    
    conn = sqlite3.connect(DB_PATH)
    
    # Drop existing table if exists
    conn.execute("DROP TABLE IF EXISTS auction_prices")
    
    # Create table
    conn.execute("""
        CREATE TABLE auction_prices (
            player_name TEXT,
            year INTEGER,
            role TEXT,
            country TEXT,
            price REAL,
            notes TEXT
        )
    """)
    
    # Insert data
    count = 0
    for _, row in auction_df.iterrows():
        try:
            price = convert_to_cr(row['Price'])
            
            conn.execute("""
                INSERT INTO auction_prices 
                (player_name, year, role, country, price, notes)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                str(row['Player']), 
                int(row['Year']) if pd.notna(row['Year']) else 0,
                str(row['Role']) if pd.notna(row['Role']) else '',
                str(row['Country']) if pd.notna(row['Country']) else '',
                price,
                str(row['Notes']) if pd.notna(row['Notes']) else ''
            ))
            count += 1
        except Exception as e:
            print(f"Error inserting {row['Player']}: {e}")
    
    conn.commit()
    print(f"✅ Imported {count} auction price records")
    conn.close()

def import_bid_data():
    """Import bidding history"""
    
    file_path = os.path.join(DATA_FOLDER, "All bids.xlsx")
    print(f"Reading: {file_path}")
    
    bid_df = pd.read_excel(file_path, sheet_name="Sheet 1", header=1)
    bid_df.columns = ['Year', 'Player', 'Role', 'Country', 'Bids', 'Last Bid', 'Result']
    bid_df = bid_df.dropna(subset=['Player'])
    
    conn = sqlite3.connect(DB_PATH)
    
    conn.execute("DROP TABLE IF EXISTS bid_history")
    conn.execute("""
        CREATE TABLE bid_history (
            year INTEGER,
            player_name TEXT,
            role TEXT,
            country TEXT,
            bids INTEGER,
            last_bid TEXT,
            result TEXT
        )
    """)
    
    count = 0
    for _, row in bid_df.iterrows():
        try:
            conn.execute("""
                INSERT INTO bid_history 
                (year, player_name, role, country, bids, last_bid, result)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                int(row['Year']) if pd.notna(row['Year']) else 0,
                str(row['Player']),
                str(row['Role']) if pd.notna(row['Role']) else '',
                str(row['Country']) if pd.notna(row['Country']) else '',
                int(row['Bids']) if pd.notna(row['Bids']) else 0,
                str(row['Last Bid']) if pd.notna(row['Last Bid']) else '',
                str(row['Result']) if pd.notna(row['Result']) else ''
            ))
            count += 1
        except Exception as e:
            print(f"Error inserting {row['Player']}: {e}")
    
    conn.commit()
    print(f"✅ Imported {count} bid history records")
    conn.close()

def import_player_metadata():
    """Import player metadata (age, status)"""
    
    file_path = os.path.join(DATA_FOLDER, "Players.xlsx")
    print(f"Reading: {file_path}")
    
    players_df = pd.read_excel(file_path, sheet_name="Sheet 1", header=1)
    players_df.columns = ['Player Name', 'Role', 'Season', 'Age', 'Status']
    players_df = players_df.dropna(subset=['Player Name'])
    
    conn = sqlite3.connect(DB_PATH)
    
    conn.execute("DROP TABLE IF EXISTS player_metadata")
    conn.execute("""
        CREATE TABLE player_metadata (
            player_name TEXT,
            season INTEGER,
            role TEXT,
            age TEXT,
            status TEXT
        )
    """)
    
    count = 0
    for _, row in players_df.iterrows():
        try:
            conn.execute("""
                INSERT INTO player_metadata 
                (player_name, season, role, age, status)
                VALUES (?, ?, ?, ?, ?)
            """, (
                str(row['Player Name']),
                int(row['Season']) if pd.notna(row['Season']) else 0,
                str(row['Role']) if pd.notna(row['Role']) else '',
                str(row['Age']) if pd.notna(row['Age']) else '',
                str(row['Status']) if pd.notna(row['Status']) else ''
            ))
            count += 1
        except Exception as e:
            print(f"Error inserting {row['Player Name']}: {e}")
    
    conn.commit()
    print(f"✅ Imported {count} player metadata records")
    conn.close()

def update_player_auction_stats_prices():
    """Update the main player_auction_stats table with prices"""
    conn = sqlite3.connect(DB_PATH)
    
    # Get latest prices (2025) from auction_prices
    conn.execute("""
        UPDATE player_auction_stats 
        SET price = (
            SELECT price FROM auction_prices 
            WHERE auction_prices.player_name LIKE '%' || player_auction_stats.player_name || '%'
            AND auction_prices.year = 2025
            LIMIT 1
        )
        WHERE EXISTS (
            SELECT 1 FROM auction_prices 
            WHERE auction_prices.player_name LIKE '%' || player_auction_stats.player_name || '%'
            AND auction_prices.year = 2025
        )
    """)
    
    updated = conn.total_changes
    conn.commit()
    print(f"✅ Updated {updated} player records with prices")
    conn.close()

if __name__ == "__main__":
    print("Importing auction data...")
    import_auction_prices()
    import_bid_data()
    import_player_metadata()
    update_player_auction_stats_prices()
    print("\n✅ All data imported successfully!")