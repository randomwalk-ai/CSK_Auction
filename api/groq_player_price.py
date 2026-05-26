"""
Groq lookup for IPL 2026 CSK player prices when scrape/DB has no row.
Used only for players on the official roster — not for squad membership.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.getenv("GROQ_SQUAD_MODEL", "llama-3.3-70b-versatile")
_groq_price_cache: Dict[str, Tuple[Dict, datetime]] = {}
CACHE_TTL_SECONDS = 86400


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def fetch_ip2026_csk_price_via_groq(
    groq_api_key: str,
    player_name: str,
    *,
    role: str = "",
    acquisition: str = "retained",
    team: str = "CSK",
    year: int = 2026,
    model: str = DEFAULT_MODEL,
    cache: Optional[Dict] = None,
) -> Dict[str, Any]:
    """
    Ask Groq for the player's IPL 2026 CSK price (retention slab / auction / trade).
    Returns estimated price with confidence — not treated as scrape-verified.
    """
    default = {
        "found": False,
        "price_cr": None,
        "confidence": "low",
        "source_note": "",
    }
    if not groq_api_key or not player_name:
        return default

    cache_store = cache if cache is not None else _groq_price_cache
    key = _norm(f"{year}:{team}:{player_name}")
    if key in cache_store:
        cached, ts = cache_store[key]
        if datetime.now() - ts < timedelta(seconds=CACHE_TTL_SECONDS):
            return cached

    prompt = f"""You are an IPL auction fact assistant. Use ONLY publicly reported IPL {year} data.

Player: {player_name}
Team: Chennai Super Kings (CSK)
Role: {role or "unknown"}
Expected acquisition: {acquisition} (retained / auction / trade / rtm)

What is this player's OFFICIAL IPL {year} price at CSK (in Crores INR)?
- For retained: retention slab amount for IPL {year}
- For trade: reported trade fee (e.g. Sanju Samson from RR)
- For auction: hammer price at IPL {year} mega/mini auction
- Do NOT use IPL 2025 prices unless unchanged and explicitly reported for 2026
- Jadeja and Pathirana are NOT on CSK {year}

Return ONLY JSON:
{{
  "found": true,
  "price_cr": 12.0,
  "acquisition": "retained|auction|trade|rtm",
  "confidence": "high|medium|low",
  "source_note": "one line citing public report type (e.g. Sportstar squad list)"
}}"""

    try:
        # trust_env=False avoids corporate/http_proxy 403 on api.groq.com
        with httpx.Client(timeout=25.0, trust_env=False) as client:
            resp = client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {groq_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.05,
                    "max_tokens": 220,
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            content = re.sub(r"```json|```", "", content).strip()
            parsed = json.loads(content)

        if not parsed.get("found") or parsed.get("price_cr") is None:
            cache_store[key] = (default, datetime.now())
            return default

        price = round(float(parsed["price_cr"]), 2)
        if price <= 0 or price > 30:
            cache_store[key] = (default, datetime.now())
            return default

        conf = str(parsed.get("confidence") or "low").lower()
        if conf not in ("high", "medium", "low"):
            conf = "low"

        result = {
            "found": True,
            "price_cr": price,
            "acquisition": parsed.get("acquisition") or acquisition,
            "confidence": conf,
            "source_note": (parsed.get("source_note") or "Groq IPL 2026 public data")[:200],
            "model": model,
        }
        cache_store[key] = (result, datetime.now())
        return result

    except Exception as e:
        logger.warning("Groq price lookup failed for %s: %s", player_name, e)
        cache_store[key] = (default, datetime.now())
        return default


def fetch_missing_prices_batch_via_groq(
    groq_api_key: str,
    players: list,
    *,
    team: str = "CSK",
    year: int = 2026,
    model: str = DEFAULT_MODEL,
) -> Dict[str, Dict[str, Any]]:
    """
    One Groq call for all players missing scrape prices.
    Returns dict keyed by normalized player name.
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not groq_api_key or not players:
        return out

    roster_lines = [
        f"- {p.get('name')}: {p.get('acquisition', 'retained')} ({p.get('role', '')})"
        for p in players
    ]
    prompt = f"""IPL {year} Chennai Super Kings (CSK) squad — report OFFICIAL price in Crores INR for each player below.
Use only IPL {year} retention slabs, trade fees, or auction hammer prices from public reports.
Do NOT use 2025 prices unless unchanged for {year}.

Players:
{chr(10).join(roster_lines)}

Return ONLY JSON:
{{
  "players": [
    {{"name": "Full Name", "price_cr": 12.0, "confidence": "high|medium|low", "source_note": "brief"}}
  ]
}}"""

    try:
        with httpx.Client(timeout=60.0, trust_env=False) as client:
            resp = client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {groq_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.05,
                    "max_tokens": 2500,
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            content = re.sub(r"```json|```", "", content).strip()
            parsed = json.loads(content)

        for row in parsed.get("players") or []:
            name = (row.get("name") or "").strip()
            if not name or row.get("price_cr") is None:
                continue
            try:
                price = round(float(row["price_cr"]), 2)
            except (TypeError, ValueError):
                continue
            if price <= 0 or price > 30:
                continue
            key = _norm(name)
            entry = {
                "found": True,
                "price_cr": price,
                "confidence": str(row.get("confidence") or "medium").lower(),
                "source_note": (row.get("source_note") or "Groq batch IPL 2026 lookup")[:200],
                "model": model,
            }
            out[key] = entry
            _groq_price_cache[_norm(f"{year}:{team}:{name}")] = (entry, datetime.now())
    except Exception as e:
        logger.warning("Groq batch price lookup failed: %s", e)

    return out
