"""
Structured model observability for valuation / bid logic.

Designed for logs, scripts, and optional API (`?include_observability=1`) — not dashboard UI.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

ML_R2_TRUST_THRESHOLD = 0.25
CONFIDENCE_WARN_THRESHOLD = 50
MATCHES_THIN_THRESHOLD = 25


def _flag(code: str, message: str, severity: str = "warn") -> Dict[str, str]:
    return {"code": code, "message": message, "severity": severity}


def ml_model_health(ml_metrics: Optional[Dict]) -> Dict[str, Any]:
    if not ml_metrics:
        return {"ready": False, "trusted": False, "reason": "ml_not_loaded"}
    r2 = float(ml_metrics.get("r2") or 0)
    mae = float(ml_metrics.get("mae_cr") or 0)
    n = int(ml_metrics.get("n_samples") or 0)
    trusted = r2 >= ML_R2_TRUST_THRESHOLD and n >= 80
    return {
        "ready": True,
        "trusted": trusted,
        "n_samples": n,
        "mae_cr": mae,
        "r2": r2,
        "reason": "ok" if trusted else "low_r2_or_small_sample",
    }


def build_valuation_observability(
    *,
    player: Dict[str, Any],
    metadata: Dict[str, Any],
    role: str,
    role_detail: str,
    risk: Dict[str, Any],
    market: Dict[str, Any],
    scarcity: float,
    market_premium: float,
    effective_z: float,
    raw_z: float,
    elite: bool,
    age_mult: float,
    age_upside: bool,
    form_norm: float,
    form_bucket: str,
    form_adj: float,
    csk_fit: int,
    csk_adj: float,
    rule_median: float,
    ml_price: Optional[float],
    ml_conf: int,
    rule_median_pre_floor: Optional[float] = None,
    w_ml: float,
    pricing_method: str,
    hist: Optional[float],
    hist_weight: float,
    shrink_weight: float,
    prior_anchor: float,
    median: float,
    verdict: str,
    ml_metrics: Optional[Dict] = None,
) -> Dict[str, Any]:
    matches = int(player.get("matches_played") or 0)
    flags: List[Dict[str, str]] = []

    if matches < MATCHES_THIN_THRESHOLD:
        flags.append(_flag(
            "thin_sample",
            f"Only {matches} IPL matches — z-score and ML weight dampened",
        ))
    if risk.get("confidence", 0) < CONFIDENCE_WARN_THRESHOLD:
        flags.append(_flag(
            "low_confidence",
            f"Rule confidence {risk.get('confidence')}% — wide price band expected",
        ))
    if not hist:
        flags.append(_flag("no_auction_clear", "No prior IPL auction price in DB for this name"))
    if ml_metrics and not ml_model_health(ml_metrics).get("trusted"):
        flags.append(_flag(
            "ml_untrusted",
            f"ML model R²={ml_metrics.get('r2')} — blend weight should stay low",
            "info",
        ))
    pre_floor = rule_median_pre_floor if rule_median_pre_floor is not None else rule_median
    if pre_floor < 0.20:
        flags.append(_flag(
            "priced_at_floor",
            f"Rule path was ₹{pre_floor:.2f} Cr before ₹0.20 Cr minimum",
        ))

    ml_health = ml_model_health(ml_metrics)
    drivers: List[str] = []
    if scarcity >= 1.2:
        drivers.append(f"role_scarcity×{scarcity:.2f}")
    if abs(effective_z) >= 0.5:
        drivers.append(f"performance_z={effective_z:+.2f}")
    if csk_fit >= 65:
        drivers.append(f"csk_fit={csk_fit}%")
    if hist:
        drivers.append(f"auction_anchor=₹{hist} Cr (w={hist_weight:.0%})")
    if w_ml > 0 and ml_price:
        drivers.append(f"ml_blend={w_ml:.0%}→₹{ml_price} Cr")

    return {
        "engine": "franchise_v2",
        "pricing_method": pricing_method,
        "role_inferred": role,
        "role_detail": role_detail,
        "confidence": {
            "overall_pct": risk.get("confidence"),
            "volatility": risk.get("volatility"),
            "experience_factor": risk.get("experience_factor"),
            "ml_confidence_pct": ml_conf if ml_price else None,
        },
        "data_quality": {
            "matches_played": matches,
            "has_historical_auction": bool(hist),
            "elite_player": elite,
            "cohort_z_raw": round(raw_z, 3),
            "cohort_z_effective": round(effective_z, 3),
            "flags": flags,
        },
        "price_decomposition": {
            "rule_median_cr": round(rule_median, 2),
            "rule_median_pre_floor_cr": (
                round(pre_floor, 2) if pre_floor < rule_median else None
            ),
            "ml_predicted_cr": ml_price,
            "ml_blend_weight": round(w_ml, 3) if ml_price else 0,
            "final_median_cr": median,
            "multipliers": {
                "scarcity": round(scarcity, 3),
                "market_premium": round(market_premium, 3),
                "age": round(age_mult, 3),
                "form_adj": round(form_adj, 3),
                "csk_adj": round(csk_adj, 3),
            },
            "shrinkage": {
                "prior_anchor_p50_cr": round(prior_anchor, 2),
                "shrink_weight": round(shrink_weight, 1),
            },
            "historical_anchor_weight": round(hist_weight, 3) if hist else 0,
        },
        "market_context": {
            "global_auction_n": market.get("count"),
            "market_mean_cr": market.get("mean"),
            "market_p50_cr": market.get("p50"),
            "market_std_cr": market.get("std"),
        },
        "ml_model": ml_health,
        "verdict_inputs": {
            "csk_fit": csk_fit,
            "median_cr": median,
            "confidence_pct": risk.get("confidence"),
            "verdict": verdict,
        },
        "top_drivers": drivers,
        "form": {
            "raw": round(float(player.get("form_rating", 0) or 0), 1),
            "normalized": round(form_norm, 1),
            "bucket": form_bucket,
        },
    }


def log_valuation_trace(player_name: str, observability: Dict[str, Any]) -> None:
    """Single-line JSON log for grep / Loki / CloudWatch."""
    if not observability:
        return
    flags = observability.get("data_quality", {}).get("flags") or []
    line = {
        "event": "valuation_observability",
        "player": player_name,
        "method": observability.get("pricing_method"),
        "median_cr": observability.get("price_decomposition", {}).get("final_median_cr"),
        "confidence_pct": observability.get("confidence", {}).get("overall_pct"),
        "flag_codes": [f.get("code") for f in flags],
        "ml_trusted": observability.get("ml_model", {}).get("trusted"),
    }
    level = logging.WARNING if flags else logging.INFO
    logger.log(level, "valuation_trace %s", json.dumps(line, default=str))


def build_bid_observability(valuation_obs: Optional[Dict], decision: Dict[str, Any]) -> Dict[str, Any]:
    """Lightweight trace for war-room heuristics (not a separate ML model)."""
    qd = decision.get("quick_decision") or {}
    return {
        "engine": "war_room_heuristic_v1",
        "valuation_pricing_method": (valuation_obs or {}).get("pricing_method"),
        "should_bid": qd.get("should_bid"),
        "strategy": qd.get("strategy"),
        "confidence_pct": qd.get("confidence_pct"),
        "inputs": {
            "fmv_cr": qd.get("fair_market_value_cr"),
            "walk_away_cr": qd.get("walk_away_cr"),
            "csk_fit": decision.get("player_analysis", {}).get("csk_fit_score"),
            "form": decision.get("player_analysis", {}).get("form_score"),
        },
        "data_sources": decision.get("data_sources") or [],
        "reason_count": len(decision.get("reasons") or []),
    }
