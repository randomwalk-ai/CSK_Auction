# CSK Auction Intelligence

IPL auction planning toolkit for **Chennai Super Kings** — franchise valuations, bid history (2018–2026), auction pool scout/arena UI, and player portraits.


## Features

- **Franchise valuation engine** — rule-based + optional ML, Groq-backed player metadata
- **Auction pool API** — full IPL roster for a given year (default **2026**)
- **Dashboard** — search, valuations, CSK squad gaps, **Arena** (floating bid-sized bubbles)
- **Player portraits** — official IPL face-card URLs from [iplt20.com](https://www.iplt20.com) squads (no image blobs in DB); initials when no squad match
- **Cricbuzz pipelines** — auction results and per-team bid sheets (Playwright scrapers)

## Prerequisites

- Python 3.10+
- SQLite database `auction_data.db` at repo root (**not committed** — see below)
- Optional: [Groq API key](https://console.groq.com/keys) for age/metadata and verdicts

## Quick start

```bash
git clone git@github.com:randomwalk-ai/CSK_Auction.git
cd CSK_Auction

python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env — set GROQ_API_KEY (optional but recommended)

# You need auction_data.db locally (build or obtain from team)
# python run_pipeline.py

./start.sh
```

Open **http://127.0.0.1:8080** (do not open `dashboard/index.html` via `file://`).

| Service    | URL |
|-----------|-----|
| Dashboard | http://127.0.0.1:8080 |
| API       | http://127.0.0.1:8000 |
| Health    | http://127.0.0.1:8000/api/health |

```bash
curl http://127.0.0.1:8000/api/health
curl "http://127.0.0.1:8000/api/players/auction-pool?filter=all&year=2026&limit=5"
```

Full runbook: **[RUN.md](RUN.md)** (Cricbuzz scrapers, ML training, troubleshooting).

## Database

`auction_data.db` is **gitignored** (~90MB+). Each developer:

1. Runs `python run_pipeline.py` after Cricsheet/CSV data is present, or  
2. Receives a copy from the team (set `DB_PATH` in `.env` if stored elsewhere)

Main tables: `auction_prices_full`, `bid_history`, `player_auction_stats`, `player_ipl_facecards`.

**Do not** use `api/auction_data.db` — that path is empty; the app uses the **repo root** database.

## Auction pool vs historical catalog

| Scope | Approx. count | Used for |
|--------|----------------|----------|
| **Single auction year** (e.g. 2026) | ~329 distinct players | Arena, scout, portrait seeding |
| **All years** in `auction_prices_full` | ~950+ distinct names | Historical analysis, not default UI |

Change the active year in `api/auction_constants.py` (`IPL_AUCTION_YEAR`) and re-import/scrape that season.

## IPL face cards (portraits)

Uniform headshots from IPL CDN (`documents.iplt20.com`), stored as **URLs only** in `player_ipl_facecards`.

```bash
# After squads are published on iplt20.com
python3 scripts/seed_ipl_facecards.py --dry-run --player "Virat Kohli"
python3 scripts/seed_ipl_facecards.py --apply
```

**Limitation:** Players not yet on a team squad page (e.g. at auction before squads load) will show **initials** until squads update and you re-run the seed.

Arena loads `facecard_url` from `/api/players/auction-pool` (browser fetches CDN directly).

## Configuration

Copy `.env.example` → `.env`. Important variables:

| Variable | Purpose |
|----------|---------|
| `GROQ_API_KEY` | Player metadata / LLM features |
| `DB_PATH` | Override path to `auction_data.db` |
| `IPL_AUCTION_YEAR` | Set in code (`api/auction_constants.py`) |

## Project layout

```
api/                 FastAPI (app.py), valuation, auction pool, avatars
dashboard/           Static UI (Arena, scout, valuations)
scripts/             Scrapers, stats, seed_ipl_facecards.py, portraits
data/                Cricsheet JSON, Cricbuzz CSVs
database/            Schema helpers
run_pipeline.py      Build / refresh SQLite from data
start.sh             API :8000 + dashboard :8080
RUN.md               Detailed operations guide
```

## Development notes

- API entry: `api/app.py` (not deprecated `franchise_app.py`)
- Portraits: prefer `seed_ipl_facecards.py`; legacy byte-cache paths exist in `player_portrait_store.py`
- Commit from **repo root**; never commit `.env` or `*.db`

## License

Internal / org use — confirm license with RandomWalk AI before external distribution.
