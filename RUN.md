# CSK Auction Dashboard — How to Run

## Prerequisites

- Python 3.10+
- `auction_data.db` present (run pipeline once if missing)

## 1. Install dependencies

```bash
cd /Users/rithanipriyankaasr/Desktop/CSK_2/auction-data-pipeline
pip install -r requirements.txt
```

## 2. Configure Groq (recommended for correct age & verdicts)

```bash
cp .env.example .env
```

Edit `.env` and set:

```
GROQ_API_KEY=gsk_your_key_here
```

Or export in the terminal:

```bash
export GROQ_API_KEY="gsk_your_key_here"
```

## 3. Start both servers (recommended)

From the repo root:

```bash
./start.sh
```

This runs:

- **API** → http://127.0.0.1:8000 (`uvicorn app:app --reload`)
- **Dashboard** → http://127.0.0.1:8080 (`python3 -m http.server`)

Open **http://127.0.0.1:8080** in your browser (do not double-click `index.html` — `file://` blocks API calls).

Quick test:

```bash
curl http://127.0.0.1:8000/api/health
curl "http://127.0.0.1:8000/api/players/auction-pool?filter=batters&limit=5"
```

### Or start manually

**Terminal 1 — API:**

```bash
cd api
python3 -m uvicorn app:app --reload --host 127.0.0.1 --port 8000
```

**Terminal 2 — Dashboard:**

```bash
cd dashboard
python3 -m http.server 8080 --bind 127.0.0.1
```

You should see on the API:

- `Valuation engine: franchise_v2`
- `GROQ_API_KEY loaded` (if `.env` is set)

Expected valuation example:

```bash
curl "http://127.0.0.1:8000/api/players/valuation/Vaibhav%20Suryavanshi"
```

**Age 14**, **~₹2.45 Cr**, verdict **Development Buy** (with override).

## 4. Purse & Scout defaults

- Total purse is **₹125 Cr** (IPL 2026, aligned with Cricbuzz).
- Scout browse tabs use **`/api/players/auction-pool`** — players in the **2026 bid-history pool**, not all-time stat leaders (Kohli, Raina, etc.).

## 5. (Optional) Refresh stats database

Only if data is missing or outdated:

```bash
cd /Users/rithanipriyankaasr/Desktop/CSK_2/auction-data-pipeline
python run_pipeline.py
```

Then restart the API (`./start.sh` or uvicorn).

**Form scores** now weight IPL over BBL/PSL in SR, economy, runs, and wickets. Re-run stats after pipeline changes:

```bash
python scripts/calculate_stats.py
```

## 6. Scrape full IPL auction (Cricbuzz — all teams, 2008+)

Cricbuzz loads auction rows with **JavaScript**. Plain `httpx`/`requests` get 0 players. Use **Playwright**:

```bash
pip install playwright pandas
playwright install chromium

# Test one year
python scripts/scrape_cricbuzz_auction.py --years 2024

# Full scrape + database
python scripts/scrape_cricbuzz_auction.py --import-db --delay 2

python scripts/train_valuation_model.py   # optional: retrain ML
```

One URL per season (all franchises):  
`https://www.cricbuzz.com/cricket-series/ipl-{YEAR}/auction/completed`

If you get **timeout on player links**, Cricbuzz often blocks headless Chromium. Use **real Chrome**:

```bash
python scripts/scrape_cricbuzz_auction.py --years 2024 --browser chrome --headed
```

Debug failures (saves screenshot + HTML):

```bash
python scripts/scrape_cricbuzz_auction.py --years 2024 --browser chrome --debug
```

Output: `data/cricbuzz_auction_all_teams.csv` + `auction_prices_full`

## 7. Scrape “Players Targeted” → “See All Bids” (per franchise bid sheet)

This is **separate** from `/auction/completed`. On each team page (e.g. Chennai Super Kings), use the **Players Targeted** section’s **See All Bids** button to open the **All Bids** modal (Player, Bids, Last Bid, Won/Lost).

```bash
# All years 2008–2026, all teams (long run — use Chrome, ~1–3+ hours)
# All teams, 2018–2026, team-wise folders + master CSV + DB
python scripts/scrape_cricbuzz_all_bids.py --import-db --browser chrome --delay 2

# Same range explicitly
python scripts/scrape_cricbuzz_all_bids.py --from-year 2018 --to-year 2026 --import-db --browser chrome

# Test one year first
python scripts/scrape_cricbuzz_all_bids.py --years 2024 --browser chrome

# Subset
python scripts/scrape_cricbuzz_all_bids.py --years 2018,2024,2026 --teams CSK,MI --browser chrome
```

Output: `data/cricbuzz_all_bids.csv` + table `bid_history` (column `viewing_team_code` = whose bid sheet it is).

Omit `--years` to scrape **2008 through 2026**. Years with no teams page or no **See All Bids** button are skipped automatically (common before ~2018).

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Age still 27 | Restart API after adding `.env`; search **Vaibhav Suryavanshi** (override in `api/player_metadata.py`) |
| CORS / fetch errors | Use `./start.sh`; open http://127.0.0.1:8080 (not `file://`); check `curl http://127.0.0.1:8000/api/health` |
| Red “API offline” banner | Start API on port 8000 in a separate terminal or via `./start.sh` |
| Database not found | Set `DB_PATH` in `.env` to full path of `auction_data.db` |

## Project layout

| Path | Role |
|------|------|
| `api/app.py` | Main API (all endpoints) |
| `api/valuation_engine.py` | Franchise valuation logic |
| `api/player_loader.py` | IPL-first player stats |
| `api/player_metadata.py` | Age/overrides + Groq |
| `dashboard/` | Frontend UI |

Do **not** use `franchise_app.py` (deprecated).
