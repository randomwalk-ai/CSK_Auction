# Franchise valuation (merged into app.py)

All valuation logic lives in:

- `valuation_engine.py` — market model, roles, risk, CSK fit
- `player_loader.py` — IPL-first stats loading
- `app.py` — single API on **port 8000**

## Run (one server)

```bash
cd auction-data-pipeline
pip install -r requirements.txt
cp .env.example .env   # add GROQ_API_KEY
cd api
python app.py
```

## Groq API key

Set in **`auction-data-pipeline/.env`**:

```
GROQ_API_KEY=gsk_...
```

Or export before starting:

```bash
export GROQ_API_KEY="gsk_..."
python app.py
```

Check: `curl http://localhost:8000/api/health` → `"groq_configured": true`

## Endpoints

| Path | Notes |
|------|--------|
| `GET /api/players/valuation/{name}` | Franchise engine (dashboard default) |
| `GET /api/v2/players/valuation/{name}` | Same handler |
| `GET /api/players/compare?p1=&p2=` | Compare |
| `GET /api/market/distribution` | Auction percentiles |

Do **not** run `franchise_app.py` on 8001 anymore.
