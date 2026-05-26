"""
Groq lookup to verify a name belongs to a professional cricketer and suggest
canonical name + Wikipedia page title for portrait resolution.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.getenv("GROQ_SQUAD_MODEL", "llama-3.3-70b-versatile")
_groq_identity_cache: Dict[str, Tuple[Dict[str, Any], datetime]] = {}
CACHE_TTL_SECONDS = 86400 * 7
RATE_LIMIT_CACHE_SECONDS = 120
MAX_RETRIES = max(1, int(os.getenv("GROQ_IDENTITY_MAX_RETRIES", "4") or "4"))
MIN_INTERVAL_S = float(os.getenv("GROQ_IDENTITY_MIN_INTERVAL_S", "1.0") or "1.0")

_last_request_at: Optional[datetime] = None
_rate_limit_until: Optional[datetime] = None
_consecutive_429 = 0


def _groq_circuit_open() -> bool:
    return _rate_limit_until is not None and datetime.now() < _rate_limit_until


def _wait_for_interval() -> None:
    global _last_request_at
    if MIN_INTERVAL_S <= 0:
        return
    now = datetime.now()
    if _last_request_at is not None:
        elapsed = (now - _last_request_at).total_seconds()
        if elapsed < MIN_INTERVAL_S:
            time.sleep(MIN_INTERVAL_S - elapsed)
    _last_request_at = datetime.now()


def _backoff_seconds(attempt: int, resp: Optional[httpx.Response] = None) -> float:
    if resp is not None:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(float(retry_after), 1.0)
            except ValueError:
                pass
    return min(60.0, 2.0 * (2 ** attempt))


def _open_rate_limit_circuit(pause_s: float) -> None:
    global _rate_limit_until, _consecutive_429
    _consecutive_429 += 1
    pause = max(pause_s, min(300.0, 15.0 * _consecutive_429))
    _rate_limit_until = datetime.now() + timedelta(seconds=pause)
    logger.info(
        "Groq rate limit — pausing identity lookups for %.0fs (consecutive 429s: %s)",
        pause,
        _consecutive_429,
    )


def _close_rate_limit_circuit() -> None:
    global _rate_limit_until, _consecutive_429
    _consecutive_429 = 0
    _rate_limit_until = None


@dataclass(frozen=True)
class GroqIdentityResult:
    is_cricketer: bool
    canonical_name: str
    wikipedia_title: str
    confidence: str
    source_note: str
    api_ok: bool = True

    @classmethod
    def failed(cls, query_name: str = "") -> "GroqIdentityResult":
        return cls(
            is_cricketer=False,
            canonical_name=(query_name or "").strip(),
            wikipedia_title="",
            confidence="low",
            source_note="Groq API unavailable",
            api_ok=False,
        )


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _parse_groq_json(content: str) -> Dict[str, Any]:
    cleaned = re.sub(r"```json|```", "", content or "").strip()
    return json.loads(cleaned)


def verify_cricketer_via_groq(
    groq_api_key: str,
    player_name: str,
    *,
    context: str = "IPL 2026 Indian cricket auction player pool",
    cricbuzz_player_id: Optional[str] = None,
    in_auction_pool: bool = False,
    model: str = DEFAULT_MODEL,
    cache: Optional[Dict[str, Tuple[Dict[str, Any], datetime]]] = None,
) -> GroqIdentityResult:
    """
    Ask Groq whether the query name is a professional cricketer and return
    disambiguation hints for Wikipedia portrait lookup.
    """
    query = (player_name or "").strip()
    if not groq_api_key or not query:
        return GroqIdentityResult.failed(query)

    if _groq_circuit_open():
        return GroqIdentityResult.failed(query)

    cache_store = cache if cache is not None else _groq_identity_cache
    cache_key = _norm(f"{context}:{cricbuzz_player_id or ''}:{query}")
    if cache_key in cache_store:
        cached, ts = cache_store[cache_key]
        ttl = (
            RATE_LIMIT_CACHE_SECONDS
            if not cached.get("api_ok", True)
            else CACHE_TTL_SECONDS
        )
        if datetime.now() - ts < timedelta(seconds=ttl):
            return GroqIdentityResult(
                is_cricketer=bool(cached.get("is_cricketer")),
                canonical_name=str(cached.get("canonical_name") or query),
                wikipedia_title=str(cached.get("wikipedia_title") or ""),
                confidence=str(cached.get("confidence") or "low"),
                source_note=str(cached.get("source_note") or ""),
                api_ok=bool(cached.get("api_ok", True)),
            )

    pool_line = (
        "This name appears in the official IPL 2026 auction player pool."
        if in_auction_pool
        else "This name is NOT confirmed in the IPL 2026 auction pool database."
    )
    id_line = (
        f"Cricbuzz player ID linked in auction DB: {cricbuzz_player_id}."
        if cricbuzz_player_id
        else "No Cricbuzz player ID linked in auction DB."
    )

    prompt = f"""You verify cricket player identities before a portrait is downloaded.

Query name: {query}
Context: {context}
{pool_line}
{id_line}

Tasks:
1. Decide if this person is a professional cricketer (domestic/international) relevant to IPL.
2. If yes, give the most common full name spelling.
3. Give the exact English Wikipedia page title if one exists (prefer "(cricketer)" disambiguation pages).
4. If the query name is garbled/corrupted, repair it (e.g. "Ja mie Overton" → "Jamie Overton").
5. If NOT a cricketer, or you are unsure, set is_cricketer=false.

Return ONLY JSON:
{{
  "is_cricketer": true,
  "canonical_name": "Jamie Overton",
  "wikipedia_title": "Jamie Overton (cricketer)",
  "confidence": "high|medium|low",
  "source_note": "brief reason"
}}"""

    default_payload = {
        "is_cricketer": False,
        "canonical_name": query,
        "wikipedia_title": "",
        "confidence": "low",
        "source_note": "",
    }

    parsed: Dict[str, Any] = default_payload
    last_exc: Optional[Exception] = None
    try:
        with httpx.Client(timeout=25.0, trust_env=False) as client:
            for attempt in range(MAX_RETRIES):
                _wait_for_interval()
                try:
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
                            "max_tokens": 260,
                        },
                    )
                    if resp.status_code == 429:
                        pause = _backoff_seconds(attempt, resp)
                        _open_rate_limit_circuit(pause)
                        if attempt + 1 < MAX_RETRIES:
                            time.sleep(pause)
                            continue
                        last_exc = httpx.HTTPStatusError(
                            "429 Too Many Requests", request=resp.request, response=resp,
                        )
                        break
                    resp.raise_for_status()
                    _close_rate_limit_circuit()
                    content = resp.json()["choices"][0]["message"]["content"]
                    parsed = _parse_groq_json(content)
                    last_exc = None
                    break
                except httpx.HTTPStatusError as exc:
                    last_exc = exc
                    if exc.response is not None and exc.response.status_code == 429:
                        pause = _backoff_seconds(attempt, exc.response)
                        _open_rate_limit_circuit(pause)
                        if attempt + 1 < MAX_RETRIES:
                            time.sleep(pause)
                            continue
                        break
                    raise
    except Exception as exc:
        last_exc = exc

    if last_exc is not None:
        if isinstance(last_exc, httpx.HTTPStatusError) and last_exc.response is not None:
            if last_exc.response.status_code == 429:
                logger.debug("Groq rate-limited for %s (using pool fallback if available)", query)
            else:
                logger.warning("Groq identity lookup failed for %s: %s", query, last_exc)
        else:
            logger.warning("Groq identity lookup failed for %s: %s", query, last_exc)
        cache_store[cache_key] = (
            {
                "is_cricketer": False,
                "canonical_name": query,
                "wikipedia_title": "",
                "confidence": "low",
                "source_note": "Groq API unavailable",
                "api_ok": False,
            },
            datetime.now(),
        )
        return GroqIdentityResult.failed(query)

    is_cricketer = bool(parsed.get("is_cricketer"))
    canonical = str(parsed.get("canonical_name") or query).strip() or query
    wiki_title = str(parsed.get("wikipedia_title") or "").strip()
    conf = str(parsed.get("confidence") or "low").lower()
    if conf not in ("high", "medium", "low"):
        conf = "low"

    if not is_cricketer:
        conf = "low"
        wiki_title = ""

    payload = {
        "is_cricketer": is_cricketer,
        "canonical_name": canonical,
        "wikipedia_title": wiki_title,
        "confidence": conf,
        "source_note": str(parsed.get("source_note") or "")[:200],
        "api_ok": True,
    }
    cache_store[cache_key] = (payload, datetime.now())
    return GroqIdentityResult(
        is_cricketer=is_cricketer,
        canonical_name=canonical,
        wikipedia_title=wiki_title,
        confidence=conf,
        source_note=payload["source_note"],
    )
