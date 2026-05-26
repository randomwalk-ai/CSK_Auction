"""
One-Command Pipeline Runner
Usage: python run_pipeline.py
"""

import os
import sys
import subprocess
from pathlib import Path

def run_command(cmd, description):
    """Run shell command and print status"""
    print(f"\n{'='*60}")
    print(f"📦 {description}...")
    print(f"{'='*60}")
    
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    
    if result.returncode == 0:
        print(f"✅ {description} completed successfully")
        if result.stdout:
            print(result.stdout[-500:])  # Last 500 chars
        return True
    else:
        print(f"❌ {description} failed")
        print(f"Error: {result.stderr}")
        return False

def main():
    """Main pipeline orchestrator"""
    
    print("\n" + "🎯"*30)
    print("   IPL AUCTION DATA PIPELINE - COMPLETE SETUP")
    print("🎯"*30)
    
    # Step 1: Install dependencies
    if not run_command("pip install -r requirements.txt", "Installing dependencies"):
        print("⚠️  Continuing anyway...")
    
    # Step 2: Create directories
    print("\n📁 Creating directories...")
    Path("data").mkdir(exist_ok=True)
    Path("data/ipl").mkdir(exist_ok=True)
    print("✅ Directories created")
    
    # Step 3: Download Cricsheet data
    if not run_command("python scripts/download_cricsheet.py", "Downloading Cricsheet data"):
        print("❌ Download failed. Check your internet connection.")
        sys.exit(1)
    
    # Step 4: Process JSON files to database
    if not run_command("python scripts/process_data.py", "Processing match data to database"):
        print("❌ Processing failed.")
        sys.exit(1)
    
    # Step 5: Calculate auction statistics
    if not run_command("python scripts/calculate_stats.py", "Calculating player statistics"):
        print("❌ Statistics calculation failed.")
        sys.exit(1)
    
    # Step 6: Start API server (optional)
    print("\n" + "🚀"*30)
    print("   PIPELINE COMPLETE! Your data is ready.")
    print("🚀"*30)
    
    print("\n📊 Next steps:")
    print("  1. Start API:     python api/app.py")
    print("  2. Open dashboard: Open dashboard/index.html in browser")
    print("  3. Query data:    curl http://localhost:8000/api/players/top-batsmen")
    
    # Ask if want to start API
    start_api = input("\n❓ Start API server now? (y/n): ").lower().strip()
    if start_api == 'y':
        print("\n🚀 Starting FastAPI server at http://localhost:8000")
        print("   Press Ctrl+C to stop\n")
        subprocess.run("python api/app.py", shell=True)

if __name__ == "__main__":
    main()