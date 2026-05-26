import sqlite3
import os

def init_database():
    """Initialize SQLite database with all required tables"""
    
    conn = sqlite3.connect('database/cricket.db')
    cursor = conn.cursor()
    
    # Read and execute schema
    with open('database/models.sql', 'r') as f:
        schema = f.read()
    
    cursor.executescript(schema)
    conn.commit()
    conn.close()
    print("Database initialized successfully")

if __name__ == "__main__":
    init_database()
