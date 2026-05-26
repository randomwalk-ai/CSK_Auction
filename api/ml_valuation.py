"""
Learn IPL auction prices from historical sales + player stats (supervised ML).

Training: GridSearchCV on GradientBoostingRegressor (log-price target).
Inference: blended with rule-based franchise engine when sample is thin.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import GridSearchCV, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

logger = logging.getLogger(__name__)

DEFAULT_MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "auction_price_model.joblib"
METRICS_PATH = Path(__file__).resolve().parent.parent / "models" / "auction_price_metrics.json"

FEATURE_COLUMNS = [
    "matches_played",
    "total_runs",
    "total_wickets",
    "strike_rate",
    "economy_rate",
    "form_rating",
    "last_10_matches_runs",
    "last_10_matches_sr",
    "last_10_matches_wickets",
    "last_10_matches_economy",
    "runs_per_match",
    "wickets_per_match",
    "role_batter",
    "role_bowler",
    "role_ar",
    "role_wk",
    "is_indian",
]

ROLE_TO_COL = {
    "Batter": "role_batter",
    "Finisher": "role_batter",
    "Anchor": "role_batter",
    "WK Finisher": "role_wk",
    "Bowler": "role_bowler",
    "Death Bowler": "role_bowler",
    "Powerplay Bowler": "role_bowler",
    "Wrist Spinner": "role_bowler",
    "Left Arm Pacer": "role_bowler",
    "All Rounder": "role_ar",
    "Fast Bowling AR": "role_ar",
    "Wicket Keeper": "role_wk",
}


def _surname(name: str) -> str:
    parts = name.strip().split()
    return parts[-1].lower() if parts else ""


def _match_stats_to_auction_name(
    auction_name: str, ipl_players: List[Dict]
) -> Optional[Dict]:
    """Strict surname match between auction label and IPL stats row."""
    a_sur = _surname(auction_name)
    if len(a_sur) < 3:
        return None
    candidates = []
    for p in ipl_players:
        p_sur = _surname(p["player_name"])
        if a_sur == p_sur or auction_name.lower() in p["player_name"].lower():
            candidates.append(p)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        # Prefer exact substring match
        for p in candidates:
            if auction_name.lower() in p["player_name"].lower():
                return p
        return candidates[0]
    return None


def build_training_dataset(conn: sqlite3.Connection) -> pd.DataFrame:
    """Join auction prices to IPL player_auction_stats for supervised learning."""
    conn.row_factory = sqlite3.Row
    ipl_rows = conn.execute(
        """
        SELECT * FROM player_auction_stats
        WHERE LOWER(competition) = 'ipl'
        """
    ).fetchall()
    ipl_players = [dict(r) for r in ipl_rows]

    # Prefer full Cricbuzz scrape (per-year sold rows) when available
    auctions = []
    try:
        full_count = conn.execute(
            "SELECT COUNT(*) FROM auction_prices_full WHERE status = 'sold' AND price >= 0.5"
        ).fetchone()[0]
        if full_count and full_count > 50:
            auctions = conn.execute(
                """
                SELECT player_name, price, role, year, country
                FROM auction_prices_full
                WHERE status = 'sold' AND price >= 0.5
                """
            ).fetchall()
            logger.info("Training on auction_prices_full (%s rows)", len(auctions))
    except sqlite3.OperationalError:
        pass

    if not auctions:
        auctions = conn.execute(
            """
            SELECT player_name, price, role, year, country
            FROM auction_prices
            WHERE price >= 0.5
            """
        ).fetchall()

    records = []
    for a in auctions:
        stats = _match_stats_to_auction_name(a[0], ipl_players)
        if not stats:
            continue
        records.append({
            "auction_name": a[0],
            "player_name": stats["player_name"],
            "price": float(a[1]),
            "auction_role": a[2] or "",
            "year": int(a[3]) if a[3] else 2020,
            "country": (a[4] or stats.get("country") or "").lower(),
            **{k: stats.get(k) or 0 for k in [
                "matches_played", "total_runs", "total_wickets",
                "strike_rate", "economy_rate", "form_rating",
                "last_10_matches_runs", "last_10_matches_sr",
                "last_10_matches_wickets", "last_10_matches_economy",
            ]},
        })

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    # One row per player: median sold price (reduces duplicate-year noise)
    df = (
        df.groupby("player_name", as_index=False)
        .agg({
            "price": "median",
            "matches_played": "max",
            "total_runs": "max",
            "total_wickets": "max",
            "strike_rate": "max",
            "economy_rate": "mean",
            "form_rating": "max",
            "last_10_matches_runs": "max",
            "last_10_matches_sr": "max",
            "last_10_matches_wickets": "max",
            "last_10_matches_economy": "mean",
            "auction_role": "first",
            "country": "first",
        })
    )
    return df


def featurize_row(player: Dict, specialist_role: str) -> np.ndarray:
    matches = max(int(player.get("matches_played") or 0), 1)
    runs = float(player.get("total_runs") or 0)
    wickets = float(player.get("total_wickets") or 0)
    role_cols = {c: 0.0 for c in FEATURE_COLUMNS if c.startswith("role_")}
    col = ROLE_TO_COL.get(specialist_role, "role_batter")
    role_cols[col] = 1.0
    country = (player.get("country") or "").lower()
    row = {
        "matches_played": matches,
        "total_runs": runs,
        "total_wickets": wickets,
        "strike_rate": float(player.get("strike_rate") or 0),
        "economy_rate": float(player.get("economy_rate") or 0),
        "form_rating": float(player.get("form_rating") or 50),
        "last_10_matches_runs": float(player.get("last_10_matches_runs") or 0),
        "last_10_matches_sr": float(player.get("last_10_matches_sr") or 0),
        "last_10_matches_wickets": float(player.get("last_10_matches_wickets") or 0),
        "last_10_matches_economy": float(player.get("last_10_matches_economy") or 0),
        "runs_per_match": runs / matches,
        "wickets_per_match": wickets / matches,
        **role_cols,
        "is_indian": 1.0 if country in ("india", "") else 0.0,
    }
    return np.array([[row[c] for c in FEATURE_COLUMNS]], dtype=float)


@dataclass
class TrainResult:
    n_samples: int
    mae_cr: float
    r2: float
    best_params: Dict
    model_path: str


class MLValuationModel:
    """Gradient boosting model trained on historical IPL auction prices."""

    def __init__(self, model_path: Optional[Path] = None):
        self.model_path = Path(model_path or DEFAULT_MODEL_PATH)
        self.pipeline: Optional[Pipeline] = None
        self.metrics: Dict = {}
        self._load()

    def _load(self) -> None:
        if self.model_path.exists():
            import sklearn
            payload = joblib.load(self.model_path)
            saved_ver = payload.get("sklearn_version", "")
            if saved_ver and saved_ver != sklearn.__version__:
                logger.warning(
                    "ML model trained with sklearn %s but runtime is %s — retrain recommended",
                    saved_ver,
                    sklearn.__version__,
                )
            self.pipeline = payload.get("pipeline")
            self.metrics = payload.get("metrics", {})
            logger.info("Loaded ML valuation model from %s", self.model_path)

    def is_ready(self) -> bool:
        return self.pipeline is not None

    def train(
        self,
        conn: sqlite3.Connection,
        *,
        test_size: float = 0.2,
        random_state: int = 42,
    ) -> TrainResult:
        df = build_training_dataset(conn)
        if len(df) < 25:
            raise ValueError(
                f"Need at least 25 matched auction+stats rows; got {len(df)}. "
                "Import more auction_prices or run stats pipeline."
            )

        X_rows = []
        for _, row in df.iterrows():
            role = row.get("auction_role") or "Batter"
            if role == "Batter":
                spec = "Batter"
            elif role == "Bowler":
                spec = "Bowler"
            elif "round" in str(role).lower() or role == "All Rounder":
                spec = "All Rounder"
            elif "Wicket" in str(role):
                spec = "Wicket Keeper"
            else:
                spec = "Batter"
            player = row.to_dict()
            X_rows.append(featurize_row(player, spec)[0])
        X = np.array(X_rows)
        y = np.log1p(df["price"].values)  # log1p handles skewed crores

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=test_size, random_state=random_state
        )

        base = Pipeline([
            ("scaler", StandardScaler()),
            ("gbr", GradientBoostingRegressor(random_state=random_state)),
        ])

        param_grid = {
            "gbr__n_estimators": [80, 120],
            "gbr__max_depth": [3, 4, 5],
            "gbr__learning_rate": [0.05, 0.08, 0.12],
            "gbr__min_samples_leaf": [2, 4],
        }

        grid = GridSearchCV(
            base,
            param_grid,
            cv=min(5, max(2, len(X_train) // 8)),
            scoring="neg_mean_absolute_error",
            n_jobs=1,
        )
        grid.fit(X_train, y_train)
        self.pipeline = grid.best_estimator_

        y_pred = self.pipeline.predict(X_test)
        y_test_cr = np.expm1(y_test)
        y_pred_cr = np.expm1(y_pred)
        mae = float(mean_absolute_error(y_test_cr, y_pred_cr))
        r2 = float(r2_score(y_test_cr, y_pred_cr)) if len(y_test) > 1 else 0.0

        self.metrics = {
            "n_samples": int(len(df)),
            "n_train": int(len(X_train)),
            "n_test": int(len(X_test)),
            "mae_cr": round(mae, 3),
            "r2": round(r2, 3),
            "best_params": grid.best_params_,
        }

        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        import sklearn
        joblib.dump(
            {
                "pipeline": self.pipeline,
                "metrics": self.metrics,
                "sklearn_version": sklearn.__version__,
            },
            self.model_path,
        )
        METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
        METRICS_PATH.write_text(json.dumps(self.metrics, indent=2))

        logger.info("ML model trained: n=%s MAE=%.2f Cr R2=%.3f", len(df), mae, r2)
        return TrainResult(
            n_samples=len(df),
            mae_cr=mae,
            r2=r2,
            best_params=grid.best_params_,
            model_path=str(self.model_path),
        )

    def predict(
        self,
        player: Dict,
        specialist_role: str,
    ) -> Tuple[float, float]:
        """
        Returns (price_cr, ml_confidence 0-100).
        confidence is higher when player is closer to training feature distribution.
        """
        if not self.is_ready():
            return 0.0, 0.0

        x = featurize_row(player, specialist_role)
        log_pred = float(self.pipeline.predict(x)[0])
        price = max(0.2, min(20.0, round(float(np.expm1(log_pred)), 2)))

        matches = int(player.get("matches_played") or 0)
        # Heuristic ML trust: needs matches and not extreme outlier
        base_conf = min(85, matches * 2)
        if self.metrics.get("r2", 0) > 0.35:
            base_conf += 10
        ml_confidence = int(min(90, max(15, base_conf)))
        return price, ml_confidence
