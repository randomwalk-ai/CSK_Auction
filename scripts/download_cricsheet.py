import requests
import zipfile
import json
import os
from pathlib import Path
from datetime import datetime
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class CricsheetDownloader:
    def __init__(self, data_dir="data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(exist_ok=True)
        
        # Competitions relevant for IPL auction
        self.competitions = {
            "ipl": "https://cricsheet.org/downloads/ipl_json.zip",
            "t20s": "https://cricsheet.org/downloads/t20s_male_json.zip",
            "smat": "https://cricsheet.org/downloads/smat_male_json.zip",
            "bbl": "https://cricsheet.org/downloads/bbl_male_json.zip",
            "psl": "https://cricsheet.org/downloads/psl_male_json.zip",
            "cpl": "https://cricsheet.org/downloads/cpl_male_json.zip",
            "sa20": "https://cricsheet.org/downloads/sa20_male_json.zip"
        }
        
    def download_competition(self, name, url):
        """Download and extract competition data"""
        logger.info(f"Downloading {name} from {url}")
        
        try:
            # Download zip file
            response = requests.get(url, timeout=60)
            response.raise_for_status()
            
            # Save zip file
            zip_path = self.data_dir / f"{name}.zip"
            with open(zip_path, 'wb') as f:
                f.write(response.content)
            
            # Extract
            extract_dir = self.data_dir / name
            extract_dir.mkdir(exist_ok=True)
            
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(extract_dir)
            
            logger.info(f"Successfully downloaded and extracted {name}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to download {name}: {e}")
            return False
    
    def download_people_csv(self):
        """Download player registry"""
        url = "https://cricsheet.org/downloads/people.csv"
        response = requests.get(url)
        
        csv_path = self.data_dir / "people.csv"
        with open(csv_path, 'wb') as f:
            f.write(response.content)
        
        logger.info("Downloaded people.csv")
        return csv_path
    
    def run_all(self):
        """Download all competitions"""
        logger.info("Starting download of all Cricsheet data...")
        
        for comp_name, url in self.competitions.items():
            self.download_competition(comp_name, url)
        
        self.download_people_csv()
        logger.info("All downloads complete!")

if __name__ == "__main__":
    downloader = CricsheetDownloader()
    downloader.run_all()