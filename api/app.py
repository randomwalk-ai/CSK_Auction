import hashlib
import os
import sqlite3
import pandas as pd
import httpx
import json
import re
import logging
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass, field

from fastapi import FastAPI, Query, HTTPException, status, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from player_loader import find_player_by_fuzzy_name, get_player_stats
from player_metadata import fetch_metadata as fetch_player_metadata
from valuation_engine import (
    AuctionMarketModel,
    FranchiseValuationEngine,
    RoleInferenceEngine,
)
from war_room import (
    build_war_room_decision,
    csk_strategy_summary,
    squad_gap_from_list,
)
from csk_squad_groq import fetch_csk_squad_via_groq
from csk_squad_catalog import load_official_csk_squad_2026, fetch_full_csk_squad_2026, filter_to_official_roster
from auction_constants import IPL_PURSE_CR, IPL_AUCTION_YEAR
from auction_pool import auction_pool_players, auction_pool_meta

# Load .env — checked in order (first match wins)
def _load_dotenv() -> None:
    from env_loader import load_project_dotenv

    loaded, _ = load_project_dotenv(override=False)
    if loaded:
        logging.getLogger(__name__).info("Loaded env from %s", loaded)

_load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="CSK Auction Dashboard API",
    description="IPL auction data + franchise valuation engine",
    version="2.0.0",
)

# --- Configuration from Environment Variables ---
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DB_PATH = os.getenv("DB_PATH", "/Users/rithanipriyankaasr/Desktop/CSK_2/auction-data-pipeline/auction_data.db")
LLM_MODEL = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.05"))
LLM_CACHE_TTL_SECONDS = int(os.getenv("LLM_CACHE_TTL_SECONDS", "86400")) # Default to 24 hours

_llm_cache: Dict[str, Tuple[Dict, datetime]] = {}

# Enable CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_role_engine = RoleInferenceEngine()

@dataclass
class SquadConfig:
    max_squad_size: int = 25
    min_squad_size: int = 18
    max_overseas_players: int = 8
    min_indian_players: int = 10
    ideal_role_breakdown: Dict[str, int] = field(default_factory=lambda: {
        "Batter": 6,
        "Bowler": 6,
        "All Rounder": 5,
        "Wicket Keeper": 2
    })
    ideal_death_bowlers: int = 2
    ideal_finishers: int = 2

squad_config = SquadConfig()

# --- Helper Functions ---

def get_player_metadata(db_player_name: str, search_name: Optional[str] = None) -> Dict:
    """Curated overrides → Groq (uses search name) → defaults."""
    return fetch_player_metadata(
        db_player_name,
        search_name=search_name,
        groq_api_key=GROQ_API_KEY,
        llm_model=LLM_MODEL,
        llm_temperature=LLM_TEMPERATURE,
        cache=_llm_cache,
        cache_ttl_seconds=LLM_CACHE_TTL_SECONDS,
    )

def get_db():
    """Get database connection"""
    if not os.path.exists(DB_PATH):
        logger.error("Database not found at %s", DB_PATH)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Database not found at {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _map_role_to_squad_bucket(role: str) -> str:
    r = (role or "").strip()
    if r in ("Finisher", "Anchor", "Batter", "WK Finisher", "WK-Batter", "WK Batter", "Wicketkeeper"):
        if "WK" in r or "Wicket" in r:
            return "Wicket Keeper"
        return "Batter"
    if r in ("Death Bowler", "Powerplay Bowler", "Bowler", "Wrist Spinner", "Left Arm Pacer"):
        return "Bowler"
    if r in ("Fast Bowling AR", "All Rounder", "Allrounder", "All-Rounder"):
        return "All Rounder"
    if r == "Wicket Keeper":
        return "Wicket Keeper"
    return "Player"


def _normalize_squad_role(raw_role: str, style: str = "") -> str:
    """Map auction/bid roles to dashboard squad buckets."""
    bucket = _map_role_to_squad_bucket(raw_role)
    if bucket != "Player":
        return bucket
    s = (style or "").lower()
    if "wk" in s or "wicket" in s:
        return "Wicket Keeper"
    if "all" in s:
        return "All Rounder"
    if "bowl" in s:
        return "Bowler"
    return "Batter"


def _load_csk_squad_local(conn: sqlite3.Connection, auction_year: int = 2026) -> List[Dict]:
    """
    Build CSK squad from local DB when external ipl-okn0 service is down.
    Retained (prior year) + auction wins from bid_history for auction_year.
    """
    seen: set = set()
    squad: List[Dict] = []
    prior_year = auction_year - 1

    retained_rows = conn.execute(
        """
        SELECT player_name, role, country, price
        FROM auction_prices_full
        WHERE team_code = 'CSK' AND year = ? AND status = 'retained' AND price IS NOT NULL
        ORDER BY price DESC
        """,
        (prior_year,),
    ).fetchall()

    for row in retained_rows:
        name = (row[0] or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        country = row[2] or "India"
        squad.append({
            "name": name,
            "role": _normalize_squad_role(row[1] or ""),
            "nationality": country,
            "country": country,
            "overseas": country.lower() not in ("", "india", "indian"),
            "price": round(float(row[3] or 0), 2),
            "retained": True,
            "source": "local_db_retained",
        })

    win_rows = conn.execute(
        """
        SELECT player_name, role, country, last_bid_cr
        FROM bid_history
        WHERE viewing_team_code = 'CSK' AND year = ? AND bid_war = 'won'
        ORDER BY last_bid_cr DESC
        """,
        (auction_year,),
    ).fetchall()

    for row in win_rows:
        name = (row[0] or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        country = row[2] or "India"
        squad.append({
            "name": name,
            "role": _normalize_squad_role(row[1] or ""),
            "nationality": country,
            "country": country,
            "overseas": country.lower() not in ("", "india", "indian"),
            "price": round(float(row[3] or 0), 2),
            "retained": False,
            "source": "local_db_auction_wins",
        })

    if not squad:
        # Fallback: latest CSK rows per player from auction_prices_full
        latest_rows = conn.execute(
            """
            SELECT player_name, role, country, price, status
            FROM auction_prices_full ap
            WHERE team_code = 'CSK'
              AND status IN ('retained', 'sold')
              AND year = (
                  SELECT MAX(year) FROM auction_prices_full
                  WHERE team_code = 'CSK' AND player_name = ap.player_name
              )
            ORDER BY price DESC
            LIMIT 25
            """
        ).fetchall()
        for row in latest_rows:
            name = (row[0] or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            country = row[2] or "India"
            squad.append({
                "name": name,
                "role": _normalize_squad_role(row[1] or ""),
                "nationality": country,
                "country": country,
                "overseas": country.lower() not in ("", "india", "indian"),
                "price": round(float(row[3] or 0), 2),
                "retained": row[4] == "retained",
                "source": "local_db_latest",
            })

    return squad


def _build_valuation(
    conn: sqlite3.Connection,
    player_name: str,
    *,
    include_observability: bool = False,
) -> Dict:
    player = get_player_stats(conn, player_name)
    if not player:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Player '{player_name}' not found in database.",
        )
    metadata = get_player_metadata(player["player_name"], search_name=player_name)
    result = FranchiseValuationEngine(conn).valuate(player, metadata)
    if result.observability:
        from model_observability import log_valuation_trace

        log_valuation_trace(player["player_name"], result.observability)
    payload = result.to_api_dict(include_observability=include_observability)
    payload["competition"] = player.get("competition", "ipl")
    payload["engine_version"] = "franchise_v2"
    return payload

# ─────────────────────────────────────────────
# API ENDPOINTS
# ─────────────────────────────────────────────

@app.get("/")
def api_root():
    """Landing page — root URL has no data; use /api/* routes or /docs."""
    return {
        "name": "CSK Auction Dashboard API",
        "version": "2.1.0",
        "message": "API is running. This is not the dashboard UI.",
        "links": {
            "interactive_docs": "/docs",
            "health": "/api/health",
            "csk_squad": "/api/csk-squad?source=auto",
            "bid_advisor": "POST /api/war-room/decision",
        },
        "dashboard_ui": {
            "how_to_run": "cd dashboard && python -m http.server 8080",
            "open_in_browser": "http://localhost:8080",
        },
    }


@app.get("/api/csk-squad")
async def get_csk_squad(
    source: str = Query(
        "auto",
        description="auto | groq | catalog | live | local — auto returns full 25-man IPL 2026 squad",
    ),
    year: int = Query(2026, description="Auction year for local bid wins"),
):
    """Fetch CSK squad — always full 25 for IPL 2026; Groq refresh when key is set."""
    conn = None
    try:
        conn = get_db()
        errors: List[str] = []

        # IPL 2026: always serve complete 25-man roster (never partial DB-only list)
        if year == 2026 and source in ("auto", "groq", "catalog", "live", "local"):
            prefer_groq = source in ("auto", "groq") and bool(GROQ_API_KEY)
            if source == "groq" and not GROQ_API_KEY:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="GROQ_API_KEY not configured — add to .env and restart API",
                )
            try:
                result = fetch_full_csk_squad_2026(
                    conn,
                    GROQ_API_KEY,
                    prefer_groq=bool(GROQ_API_KEY),
                    auction_year=year,
                )
                if result.get("squad"):
                    squad = filter_to_official_roster(result["squad"])
                    result["squad"] = squad
                    result["count"] = len(squad)
                    return result
            except Exception as e:
                errors.append(f"Full squad load failed ({e})")
                logger.warning("Full CSK 2026 squad failed: %s", e)
                if source in ("groq", "catalog"):
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="; ".join(errors),
                    ) from e

        if source in ("auto", "groq") and GROQ_API_KEY and year != 2026:
            try:
                result = fetch_csk_squad_via_groq(conn, GROQ_API_KEY, auction_year=year)
                if result.get("squad"):
                    return result
                errors.append("Groq returned empty squad")
            except Exception as e:
                errors.append(f"Groq squad fetch failed ({e})")
                logger.warning("Groq CSK squad failed: %s", e)
                if source == "groq":
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="; ".join(errors),
                    ) from e

        if source == "groq" and not GROQ_API_KEY:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="GROQ_API_KEY not configured — add to .env and restart API",
            )

        if source in ("auto", "live"):
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.get(
                        "https://ipl-okn0.onrender.com/squad/csk", timeout=10.0
                    )
                    response.raise_for_status()
                    data = response.json()

                if data.get("status_code") == 200 and data.get("squad"):
                    squad = []
                    for player_data in data["squad"].values():
                        if not player_data.get("Name"):
                            continue

                        name = player_data["Name"]
                        style = player_data.get("Style", "")
                        role = _normalize_squad_role("", style)
                        if player_data.get("Wicketkeeper"):
                            role = "Wicket Keeper"
                        elif "bowl" in style.lower() and "bat" in style.lower():
                            role = "All Rounder"
                        elif "bowl" in style.lower() or "arm" in style.lower():
                            role = "Bowler"
                        elif role == "Player":
                            role = "Batter"

                        cursor = conn.execute(
                            """
                            SELECT price FROM auction_prices
                            WHERE player_name LIKE ?
                            ORDER BY year DESC LIMIT 1
                            """,
                            (f"%{name}%",),
                        )
                        result = cursor.fetchone()
                        price = float(result[0]) if result and result[0] else 0
                        nat = player_data.get("Nationality", "IND")

                        squad.append({
                            "name": name,
                            "role": role,
                            "nationality": nat,
                            "country": nat,
                            "style": style,
                            "wicketkeeper": player_data.get("Wicketkeeper", False),
                            "overseas": player_data.get("Overseas", False),
                            "price": price,
                            "retained": True,
                        })

                    seen: set = set()
                    unique_squad = []
                    for player in squad:
                        if player["name"] not in seen:
                            seen.add(player["name"])
                            unique_squad.append(player)

                    # Live API often serves stale 2024/25 rosters — prefer 2026 official list
                    if len(unique_squad) >= 20 and year != 2026:
                        return {
                            "squad": unique_squad,
                            "count": len(unique_squad),
                            "source": "live_api",
                        }
                    errors.append(
                        f"Live API returned {len(unique_squad)} players — "
                        "may be stale; using IPL 2026 official roster instead."
                    )
            except httpx.HTTPError as e:
                errors.append(f"Live squad API unavailable ({e})")
                logger.warning("Live CSK squad fetch failed: %s", e)

        # Full IPL 2026 roster — legacy path if early return above missed
        if source in ("auto", "catalog") and year == 2026:
            try:
                catalog_result = load_official_csk_squad_2026(conn, auction_year=year)
                if catalog_result.get("squad"):
                    return catalog_result
            except Exception as e:
                errors.append(f"Official catalog load failed ({e})")

        if source in ("auto", "local") and year != 2026:
            local_squad = _load_csk_squad_local(conn, auction_year=year)
            if local_squad:
                return {
                    "squad": local_squad,
                    "count": len(local_squad),
                    "source": "local_db",
                    "note": (
                        "Loaded from local auction DB (retained + bid wins). "
                        "Live squad service was unavailable."
                        if errors
                        else "Loaded from local auction DB."
                    ),
                    "live_errors": errors,
                }
            errors.append("No CSK squad rows in local database")

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="; ".join(errors) or "Could not load CSK squad",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Unexpected error fetching squad: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error fetching squad",
        )
    finally:
        if conn:
            conn.close()

@app.get("/api/health")
@app.get("/api/v2/health")
def health_check():
    conn = None 
    try:
        conn = get_db()
        conn.execute("SELECT 1 FROM player_auction_stats LIMIT 1")
        ml_info = {"trained": False}
        try:
            from ml_valuation import MLValuationModel, METRICS_PATH
            m = MLValuationModel()
            ml_info = {
                "trained": m.is_ready(),
                "metrics": m.metrics if m.is_ready() else {},
                "metrics_file": str(METRICS_PATH) if METRICS_PATH.exists() else None,
            }
        except ImportError:
            ml_info = {"trained": False, "error": "scikit-learn not installed"}
        return {
            "status": "healthy",
            "database": "connected",
            "engine": "franchise_v2",
            "api_version": "2.1.0",
            "groq_configured": bool(GROQ_API_KEY),
            "ml_model": ml_info,
        }
    except HTTPException as e:
        logger.error("Health check failed: %s", e.detail)
        raise e
    except Exception as e:
        logger.error("Health check failed due to unexpected error: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Health check failed: {e}")
    finally:
        if conn:
            conn.close()

# ─────────────────────────────────────────────
# PLAYER STATS ENDPOINTS
# ─────────────────────────────────────────────

_PLAYER_LIST_FROM = """
    FROM player_auction_stats s
    LEFT JOIN players p ON LOWER(TRIM(s.player_name)) = LOWER(TRIM(p.name))
"""


@app.get("/api/players/top-batsmen")
def top_batsmen(limit: int = Query(20, ge=1, le=100)):
    conn = None
    try:
        conn = get_db()
        query = f"""
            SELECT s.player_name, s.matches_played, s.total_runs, s.average, s.strike_rate,
                   s.highest_score, s.fifties, s.hundreds, s.form_rating,
                   COALESCE(p.country, 'Unknown') AS country
            {_PLAYER_LIST_FROM}
            WHERE s.total_runs > 0
            ORDER BY s.total_runs DESC
            LIMIT ?
        """
        df = pd.read_sql(query, conn, params=[limit])
        return JSONResponse(content=df.to_dict("records"))
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error("Error fetching top batsmen: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error fetching top batsmen")
    finally:
        if conn:
            conn.close()

@app.get("/api/players/top-bowlers")
def top_bowlers(limit: int = Query(20, ge=1, le=100)):
    conn = None
    try:
        conn = get_db()
        query = f"""
            SELECT s.player_name, s.matches_played, s.total_wickets, s.bowling_average,
                   s.economy_rate, s.form_rating,
                   COALESCE(p.country, 'Unknown') AS country
            {_PLAYER_LIST_FROM}
            WHERE s.total_wickets > 0
            ORDER BY s.total_wickets DESC
            LIMIT ?
        """
        df = pd.read_sql(query, conn, params=[limit])
        return JSONResponse(content=df.to_dict("records"))
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error("Error fetching top bowlers: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error fetching top bowlers")
    finally:
        if conn:
            conn.close()

@app.get("/api/players/search")
def search_players(name: str = Query(..., min_length=1), limit: int = Query(30, ge=1, le=100)):
    conn = None
    try:
        conn = get_db()
        player = get_player_stats(conn, name)
        if not player:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Player '{name}' not found.")
        return JSONResponse(content=[{
            "player_name": player.get("player_name"),
            "matches_played": player.get("matches_played"),
            "total_runs": player.get("total_runs"),
            "total_wickets": player.get("total_wickets"),
            "strike_rate": player.get("strike_rate"),
            "economy_rate": player.get("economy_rate"),
            "form_rating": player.get("form_rating"),
            "last_10_matches_sr": player.get("last_10_matches_sr"),
            "last_10_matches_economy": player.get("last_10_matches_economy"),
            "competition": player.get("competition"),
        }])
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error("Error searching for player %s: %s", name, e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error searching for player")
    finally:
        if conn:
            conn.close()

@app.get("/api/players/all-rounders")
def all_rounders(min_runs: int = Query(200), min_wickets: int = Query(5), limit: int = Query(20)):
    conn = None
    try:
        conn = get_db()
        query = f"""
            SELECT s.player_name, s.matches_played, s.total_runs, s.strike_rate,
                   s.total_wickets, s.economy_rate, s.form_rating,
                   COALESCE(p.country, 'Unknown') AS country
            {_PLAYER_LIST_FROM}
            WHERE s.total_runs >= ? AND s.total_wickets >= ?
            ORDER BY (s.total_runs * 0.5 + s.total_wickets * 5) DESC
            LIMIT ?
        """
        df = pd.read_sql(query, conn, params=[min_runs, min_wickets, limit])
        return JSONResponse(content=df.to_dict("records"))
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error("Error fetching all-rounders: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error fetching all-rounders")
    finally:
        if conn:
            conn.close()

@app.get("/api/players/in-form")
def in_form_players(
    limit: int = Query(20, ge=1, le=100),
    year: int = Query(IPL_AUCTION_YEAR, description="Restrict to IPL auction pool for this year"),
    pool_only: bool = Query(True, description="When true, only players in that year's bid history"),
):
    conn = None
    try:
        conn = get_db()
        if pool_only:
            players = auction_pool_players(conn, year=year, pool_filter="inform", limit=limit)
            return JSONResponse(content=players)
        query = f"""
            SELECT s.player_name, s.form_rating, s.matches_played, s.total_runs, s.total_wickets,
                   COALESCE(p.country, 'Unknown') AS country
            {_PLAYER_LIST_FROM}
            WHERE s.form_rating IS NOT NULL AND s.form_rating > 60
            ORDER BY s.form_rating DESC
            LIMIT ?
        """
        df = pd.read_sql(query, conn, params=[limit])
        return JSONResponse(content=df.to_dict("records"))
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error("Error fetching in-form players: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error fetching in-form players")
    finally:
        if conn:
            conn.close()


@app.get("/api/players/auction-pool")
def auction_pool(
    filter: str = Query(
        "batters",
        description="batters | bowlers | allrounders | inform | all",
    ),
    year: int = Query(IPL_AUCTION_YEAR),
    limit: int = Query(48, ge=1, le=1000),
):
    """Scout/Arena pool — full IPL auction roster for the given year."""
    conn = None
    try:
        conn = get_db()
        meta = auction_pool_meta(conn, year)
        players = auction_pool_players(conn, year=year, pool_filter=filter, limit=limit)
        return {
            "year": year,
            "filter": filter,
            "count": len(players),
            "pool_size": meta.get("pool_size", 0),
            "players": players,
        }
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error("Error fetching auction pool: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error fetching auction pool")
    finally:
        if conn:
            conn.close()


from player_avatars import avatar_image_bytes, resolve_avatar, warm_portrait_cache


@app.on_event("startup")
def startup_warm_portraits() -> None:
    """Optional background portrait warm — off by default (set PORTRAIT_WARM_ON_STARTUP=1)."""
    if os.getenv("PORTRAIT_WARM_ON_STARTUP", "0").strip() not in ("1", "true", "yes"):
        logger.info(
            "Portrait warm on startup disabled (set PORTRAIT_WARM_ON_STARTUP=1 to enable)"
        )
        return
    import threading

    def _run() -> None:
        try:
            warm_portrait_cache()
        except Exception as exc:
            logger.warning("Portrait cache warm skipped: %s", exc)

    threading.Thread(target=_run, daemon=True, name="portrait-warm").start()
    logger.info("Portrait warm on startup started in background")


@app.get("/api/players/avatar")
def player_avatar(
    name: str = Query(..., min_length=1),
    fallback: str | None = Query(
        None,
        description="When 'espn', skip IPL face card and use ESPN Cricinfo only",
    ),
):
    """Portrait metadata — IPL CDN, else ESPN CDN, else same-origin /avatar/img."""
    from urllib.parse import quote

    clean = name.strip()
    skip_ipl = (fallback or "").strip().lower() in ("espn", "espncricinfo")
    url, source = resolve_avatar(clean, skip_ipl=skip_ipl)
    if source in ("iplt20", "espncricinfo") and url:
        if source == "iplt20":
            img_url = f"/api/players/facecard/img?name={quote(clean)}"
            return {
                "player_name": clean,
                "url": img_url,
                "img_url": img_url,
                "source": source,
            }
        return {
            "player_name": clean,
            "url": url,
            "img_url": url,
            "source": source,
        }
    img_url = f"/api/players/avatar/img?name={quote(clean)}"
    return {
        "player_name": clean,
        "url": img_url,
        "img_url": img_url,
        "source": source,
    }


@app.get("/api/players/facecard/img")
def player_facecard_img(name: str = Query(..., min_length=1)):
    """Same-origin proxy for IPL headshots (arena + dashboard)."""
    from fastapi.responses import Response
    from ipl_facecards import fetch_facecard_image_bytes

    clean = name.strip()
    hit = fetch_facecard_image_bytes(clean)
    if not hit:
        raise HTTPException(status_code=404, detail="No IPL face card for player")
    data, content_type = hit
    etag = hashlib.sha256(data).hexdigest()[:16]
    return Response(
        content=data,
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=86400, immutable",
            "ETag": f'"{etag}"',
            "X-Avatar-Source": "iplt20",
        },
    )


@app.get("/api/players/avatar/img")
def player_avatar_img(name: str = Query(..., min_length=1)):
    """Same-origin portrait bytes from player_portraits cache."""
    from fastapi.responses import Response

    clean = name.strip()
    data, content_type, source = avatar_image_bytes(clean)
    etag = hashlib.sha256(data).hexdigest()[:16]
    cache_control = (
        "public, max-age=86400, immutable"
        if source in ("iplt20", "espncricinfo")
        else "no-cache, max-age=0"
    )
    return Response(
        content=data,
        media_type=content_type,
        headers={
            "Cache-Control": cache_control,
            "ETag": f'"{etag}"',
            "X-Avatar-Source": source,
        },
    )


@app.get("/api/players/valuation/{player_name}")
@app.get("/api/v2/players/valuation/{player_name}")
def player_valuation(
    player_name: str,
    include_observability: bool = Query(
        False,
        description="Attach structured model trace (for scripts/ops, not dashboard UI)",
    ),
):
    conn = None
    try:
        conn = get_db()
        return _build_valuation(conn, player_name, include_observability=include_observability)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error valuating player %s: %s", player_name, e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error valuating player")
    finally:
        if conn:
            conn.close()


@app.get("/api/market/distribution")
@app.get("/api/v2/market/distribution")
def market_distribution():
    conn = None
    try:
        conn = get_db()
        model = AuctionMarketModel(conn)
        return {
            "global": model.get_market_distribution(),
            "by_role": {
                role: model.get_market_distribution(role)
                for role in ("Batter", "Bowler", "All Rounder", "Wicket Keeper")
            },
        }
    finally:
        if conn:
            conn.close()

@app.get("/api/players/compare")
@app.get("/api/v2/players/compare")
def compare_players(p1: str = Query(...), p2: str = Query(...)):
    """Compare two players side by side"""
    try:
        val1 = player_valuation(p1)
        val2 = player_valuation(p2)
        
        return {
            "player1": val1,
            "player2": val2
        }
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error("Error comparing players %s and %s: %s", p1, p2, e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error comparing players")

@app.get("/api/ml/status")
def ml_model_status():
    """Whether grid-search ML model is trained and on-disk metrics."""
    try:
        from ml_valuation import MLValuationModel, METRICS_PATH
        model = MLValuationModel()
        return {
            "trained": model.is_ready(),
            "model_path": str(model.model_path),
            "metrics": model.metrics if model.is_ready() else {},
            "metrics_file": str(METRICS_PATH) if METRICS_PATH.exists() else None,
        }
    except ImportError as e:
        return {"trained": False, "error": f"scikit-learn required: {e}"}


@app.post("/api/ml/train")
def ml_train_model():
    """Train/retrain auction price model (GridSearchCV). Run scripts/train_valuation_model.py for CLI."""
    try:
        from ml_valuation import MLValuationModel
    except ImportError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Install scikit-learn: pip install scikit-learn joblib",
        )
    conn = get_db()
    try:
        result = MLValuationModel().train(conn)
        return {
            "status": "trained",
            "n_samples": result.n_samples,
            "test_mae_cr": result.mae_cr,
            "test_r2": result.r2,
            "best_params": result.best_params,
            "model_path": result.model_path,
        }
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    finally:
        conn.close()


@app.get("/api/squad/gaps")
async def squad_gaps(retained: Optional[str] = Query(None), budget: float = Query(IPL_PURSE_CR)):
    """Get squad gaps based on current squad and ideal composition"""
    conn = None
    try:
        conn = get_db()
        retained_names = [p.strip() for p in retained.split(",")] if retained else []
        
        current_squad_details = []
        total_retained_value = 0.0
        current_role_breakdown = {"Batter": 0, "Bowler": 0, "All Rounder": 0, "Wicket Keeper": 0}
        current_overseas_players = 0
        current_indian_players = 0

        for name in retained_names:
            player_dict = get_player_stats(conn, name)
            if player_dict:
                matched_name = player_dict["player_name"]
                specialist_role, _ = _role_engine.infer(player_dict)
                role = _map_role_to_squad_bucket(specialist_role)

                current_squad_details.append({
                    "name": matched_name,
                    "role": role,
                    "specialist_role": specialist_role,
                    "country": player_dict.get("country", ""),
                })
                current_role_breakdown[role] = current_role_breakdown.get(role, 0) + 1

                country = (player_dict.get("country") or "").lower()
                if country == "india" or not country:
                    current_indian_players += 1
                else:
                    current_overseas_players += 1

                model = AuctionMarketModel(conn)
                hist = model.get_historical_price(matched_name)
                if hist and hist >= 0.5:
                    total_retained_value += hist
                else:
                    price_cursor = conn.execute(
                        """
                        SELECT price FROM auction_prices
                        WHERE player_name LIKE ? AND price >= 0.5
                        ORDER BY year DESC LIMIT 1
                        """,
                        (f"%{matched_name}%",),
                    )
                    price_result = price_cursor.fetchone()
                    total_retained_value += float(price_result[0]) if price_result and price_result[0] else 0

        remaining_budget = budget - total_retained_value
        current_squad_size = len(current_squad_details)

        gaps = []
        critical_gaps = []

        # Overall squad size
        if current_squad_size < squad_config.min_squad_size:
            gaps.append({"role": "Squad Size", "priority": "Critical", "description": f"Need {squad_config.min_squad_size - current_squad_size} more players to reach minimum squad size."})
            critical_gaps.append({"role": "Squad Size", "priority": "Critical", "description": f"Need {squad_config.min_squad_size - current_squad_size} more players to reach minimum squad size."})
        
        # Overseas players
        if current_overseas_players > squad_config.max_overseas_players:
            gaps.append({"role": "Overseas Players", "priority": "Critical", "description": f"Too many overseas players ({current_overseas_players}). Max allowed: {squad_config.max_overseas_players}."})
            critical_gaps.append({"role": "Overseas Players", "priority": "Critical", "description": f"Too many overseas players ({current_overseas_players}). Max allowed: {squad_config.max_overseas_players}."})
        
        # Indian players
        if current_indian_players < squad_config.min_indian_players:
            gaps.append({"role": "Indian Players", "priority": "High", "description": f"Need {squad_config.min_indian_players - current_indian_players} more Indian players."})
            critical_gaps.append({"role": "Indian Players", "priority": "High", "description": f"Need {squad_config.min_indian_players - current_indian_players} more Indian players."})

        # Role-based gaps
        for role, ideal_count in squad_config.ideal_role_breakdown.items():
            current_count = current_role_breakdown.get(role, 0)
            if current_count < ideal_count:
                gap_description = f"Need {ideal_count - current_count} {role}(s)."
                gaps.append({"role": role, "priority": "High", "description": gap_description})
                if ideal_count - current_count >= 2: # Example: if more than 1 player needed, make it critical
                    critical_gaps.append({"role": role, "priority": "Critical", "description": gap_description})

        # Specific skill gaps (e.g., death bowlers, finishers) - these would need more sophisticated player tagging
        # For now, this is a placeholder and would require LLM or more data to identify specific player types
        # Example: if no death bowler identified in retained squad
        # if not any(p['role'] == 'Bowler' and p.get('is_death_bowler', False) for p in current_squad_details):
        #     gaps.append({"role": "Death Bowler", "priority": "Critical", "description": "Need specialist death bowler."})

        return {
            "current_squad_size": current_squad_size,
            "total_retained_value": round(total_retained_value, 2),
            "remaining_budget": round(remaining_budget, 2),
            "current_role_breakdown": current_role_breakdown,
            "current_overseas_players": current_overseas_players,
            "current_indian_players": current_indian_players,
            "gaps": gaps,
            "critical_gaps": critical_gaps,
            "current_squad_details": current_squad_details
        }

    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error("Error calculating squad gaps: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error calculating squad gaps")
    finally:
        if conn:
            conn.close()


@app.post("/api/squad/impact")
@app.post("/api/v2/squad/impact")
async def squad_impact(payload: Dict = Body(...)):
    """
    Who a pool player fills or upgrades vs current squad (CSK fit + gaps).
    Body: { player, squad: [{name, role, price, country, retained?, price_verified?}], budget?, candidate_price_cr? }
    """
    conn = None
    try:
        player = (payload.get("player") or payload.get("player_name") or "").strip()
        if not player:
            raise HTTPException(status_code=400, detail="player required")
        squad = payload.get("squad") or []
        budget = float(payload.get("budget") or IPL_PURSE_CR)
        candidate_price = payload.get("candidate_price_cr")
        if candidate_price is not None:
            candidate_price = float(candidate_price)

        conn = get_db()

        def metadata_fn(db_name: str, search_name: Optional[str] = None) -> Dict:
            return get_player_metadata(db_name, search_name=search_name)

        from squad_impact import squad_impact_insight

        return squad_impact_insight(
            conn,
            player,
            squad,
            budget=budget,
            candidate_price_cr=candidate_price,
            metadata_fn=metadata_fn,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Squad impact error: %s", e)
        raise HTTPException(status_code=500, detail="Squad impact failed")
    finally:
        if conn:
            conn.close()


@app.get("/api/war-room/strategy")
def war_room_strategy(
    team: str = Query("CSK"),
    from_year: int = Query(2018),
    to_year: int = Query(2026),
):
    """CSK career bid-intelligence strategy summary for dashboard sidebar."""
    return csk_strategy_summary(team=team.upper(), from_year=from_year, to_year=to_year)


@app.get("/api/observability/model-health")
@app.get("/api/v2/observability/model-health")
def model_health():
    """Ops snapshot: ML trust gate, engine version, last training metrics."""
    from model_observability import ml_model_health

    ml_info: Dict = {"trained": False}
    try:
        from ml_valuation import MLValuationModel, METRICS_PATH

        m = MLValuationModel()
        if m.is_ready():
            ml_info = {"trained": True, **ml_model_health(m.metrics)}
            ml_info["metrics"] = m.metrics
        else:
            ml_info = {"trained": False, "reason": "model_not_ready"}
        ml_info["metrics_file"] = str(METRICS_PATH) if METRICS_PATH.exists() else None
    except ImportError:
        ml_info = {"trained": False, "error": "scikit-learn not installed"}
    return {
        "engine": "franchise_v2",
        "war_room": "heuristic_v1",
        "ml": ml_info,
        "observability": {
            "valuation_log_event": "valuation_trace",
            "api_flag": "include_observability=1 on /api/players/valuation/{name}",
        },
    }


@app.get("/api/war-room/decision")
def war_room_decision(
    player: str = Query(..., description="Player on the block"),
    budget: float = Query(IPL_PURSE_CR),
    current_bid: float = Query(0.0, description="Current bid in Cr (0 = pre-auction)"),
    base_price: float = Query(2.0),
    auction_year: int = Query(2026),
    retained: Optional[str] = Query(None, description="Comma-separated squad from dashboard"),
    include_observability: bool = Query(False, description="Attach valuation + bid decision trace"),
):
    """
    War Room quick-decision card: valuation + squad gaps + bid-history intelligence.
    Pass `retained` as JSON-encoded squad names or use squad_json body via POST.
    """
    conn = None
    try:
        conn = get_db()
        valuation = _build_valuation(conn, player, include_observability=include_observability)

        squad: List[Dict] = []
        if retained:
            names = [n.strip() for n in retained.split(",") if n.strip()]
            for name in names:
                stats = get_player_stats(conn, name)
                if stats:
                    specialist, _ = _role_engine.infer(stats)
                    squad.append(
                        {
                            "name": stats["player_name"],
                            "role": _map_role_to_squad_bucket(specialist),
                            "country": stats.get("country", "India"),
                            "price": 0,
                        }
                    )
        squad_state = squad_gap_from_list(squad, budget=budget)

        decision = build_war_room_decision(
            valuation,
            squad_state,
            current_bid=current_bid,
            base_price=base_price,
            auction_year=auction_year,
        )
        decision["strategy_summary"] = csk_strategy_summary()
        if include_observability:
            from model_observability import build_bid_observability

            decision["model_observability"] = {
                "valuation": valuation.get("observability"),
                "bid": build_bid_observability(valuation.get("observability"), decision),
            }
        return decision
    except HTTPException:
        raise
    except Exception as e:
        logger.error("War room decision error for %s: %s", player, e)
        raise HTTPException(status_code=500, detail="War room decision failed")
    finally:
        if conn:
            conn.close()


@app.post("/api/war-room/decision")
async def war_room_decision_post(payload: Dict = Body(...)):
    """
    War room with full squad state from dashboard localStorage.
    Body: { player, budget, current_bid, base_price, auction_year, squad: [{name, role, price, country}] }
    """
    conn = None
    try:
        player = payload.get("player") or payload.get("player_name")
        if not player:
            raise HTTPException(status_code=400, detail="player required")

        conn = get_db()
        include_obs = bool(payload.get("include_observability"))
        valuation = _build_valuation(conn, player, include_observability=include_obs)
        squad = payload.get("squad") or []
        budget = float(payload.get("budget") or IPL_PURSE_CR)
        squad_state = squad_gap_from_list(squad, budget=budget)

        decision = build_war_room_decision(
            valuation,
            squad_state,
            current_bid=float(payload.get("current_bid") or 0),
            base_price=float(payload.get("base_price") or 2.0),
            auction_year=int(payload.get("auction_year") or 2026),
        )
        decision["strategy_summary"] = csk_strategy_summary()
        if include_obs:
            from model_observability import build_bid_observability

            decision["model_observability"] = {
                "valuation": valuation.get("observability"),
                "bid": build_bid_observability(valuation.get("observability"), decision),
            }
        return decision
    except HTTPException:
        raise
    except Exception as e:
        logger.error("War room POST error: %s", e)
        raise HTTPException(status_code=500, detail="War room decision failed")
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    import uvicorn
    ml_routes = [getattr(r, "path", "") for r in app.routes if "ml" in getattr(r, "path", "")]
    logger.info("\n" + "="*50)
    logger.info("🏏 CSK AUCTION DASHBOARD API (v2.1.0)")
    logger.info("="*50)
    logger.info("📍 Database: %s", DB_PATH)
    logger.info("🌐 Server: http://localhost:8000")
    logger.info("📚 Docs: http://localhost:8000/docs")
    logger.info("🧠 Valuation engine: franchise_v2")
    logger.info("🤖 ML routes: %s", ml_routes or "NONE — wrong app.py?")
    if GROQ_API_KEY:
        logger.info("✅ GROQ_API_KEY loaded (age/verdict metadata enabled)")
    else:
        logger.info("⚠️  GROQ_API_KEY not set — using default age=27. Add to .env or export.")
    logger.info("="*50 + "\n")
    
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
