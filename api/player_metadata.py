"""
Player metadata: curated overrides, then Groq LLM, then safe defaults.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# Curated facts for players Groq often mis-guesses (abbreviated DB names, uncapped youngsters).
# Keys are normalized via _norm_key().
PLAYER_OVERRIDES: Dict[str, Dict] = {
    "v suryavanshi": {
        "full_name": "Vaibhav Suryavanshi",
        "age": 14,
        "injury_risk": "Low",
        "big_game_performer": False,
        "ipl_experience": "None",
    },
    "vaibhav suryavanshi": {
        "full_name": "Vaibhav Suryavanshi",
        "age": 14,
        "injury_risk": "Low",
        "big_game_performer": False,
        "ipl_experience": "None",
    },
}


def _norm_key(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def _default_metadata() -> Dict:
    return {
        "age": 27,
        "injury_risk": "Medium",
        "big_game_performer": False,
        "ipl_experience": "Moderate",
    }


def lookup_override(db_name: str, search_name: Optional[str] = None) -> Optional[Dict]:
    for candidate in (search_name, db_name):
        if not candidate:
            continue
        hit = PLAYER_OVERRIDES.get(_norm_key(candidate))
        if hit:
            return {**_default_metadata(), **hit}
    return None


def fetch_metadata(
    db_player_name: str,
    *,
    search_name: Optional[str] = None,
    groq_api_key: Optional[str] = None,
    llm_model: str = "llama-3.3-70b-versatile",
    llm_temperature: float = 0.05,
    cache: Optional[Dict[str, Tuple[Dict, datetime]]] = None,
    cache_ttl_seconds: int = 86400,
) -> Dict:
    """
    Resolve metadata for valuation.
    Priority: override table → Groq (full search name) → defaults.
    """
    override = lookup_override(db_player_name, search_name)
    if override:
        logger.info("Using curated metadata for %s", db_player_name)
        return override

    cache_key = _norm_key(search_name or db_player_name)
    if cache is not None and cache_key in cache:
        cached, ts = cache[cache_key]
        if datetime.now() - ts < timedelta(seconds=cache_ttl_seconds):
            return cached
        del cache[cache_key]

    default = _default_metadata()
    if not groq_api_key:
        if cache is not None:
            cache[cache_key] = (default, datetime.now())
        return default

    # Ask Groq using the user's search string when available (full name helps a lot).
    query_name = (search_name or db_player_name).strip()
    if db_player_name.lower() != query_name.lower():
        query_label = f"{query_name} (database name: {db_player_name})"
    else:
        query_label = query_name

    prompt = f"""Return ONLY valid JSON for Indian cricket player: {query_label}
{{
  "age": <int as of 2025>,
  "injury_risk": "<Low|Medium|High>",
  "big_game_performer": <true|false>,
  "ipl_experience": "<None|Limited|Moderate|Experienced>"
}}
Use only verified public facts. If uncertain about age, use null for age field."""

    try:
        with httpx.Client(timeout=12.0) as client:
            resp = client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {groq_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": llm_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": llm_temperature,
                    "max_tokens": 200,
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            content = re.sub(r"```json|```", "", content).strip()
            parsed = json.loads(content)
            if parsed.get("age") is None:
                parsed.pop("age", None)
            result = {**default, **parsed}
            if cache is not None:
                cache[cache_key] = (result, datetime.now())
            return result
    except Exception as e:
        logger.warning("LLM metadata failed for %s: %s", query_label, e)

    if cache is not None:
        cache[cache_key] = (default, datetime.now())
    return default
