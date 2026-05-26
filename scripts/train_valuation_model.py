#!/usr/bin/env python3
"""
Train ML auction price model from historical auction_prices + player_auction_stats.

Usage:
  cd auction-data-pipeline
  python scripts/train_valuation_model.py
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

from ml_valuation import MLValuationModel, METRICS_PATH, DEFAULT_MODEL_PATH

DB_PATH = os.getenv(
    "DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "auction_data.db"),
)


def main():
    if not os.path.exists(DB_PATH):
        print(f"Database not found: {DB_PATH}")
        sys.exit(1)

    try:
        import sklearn
        print(f"scikit-learn {sklearn.__version__}")
    except ImportError:
        print("Install: pip install 'scikit-learn>=1.8.0' joblib")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    model = MLValuationModel()
    try:
        result = model.train(conn)
        print("\n✅ ML valuation model trained")
        print(f"   Samples:     {result.n_samples}")
        print(f"   Test MAE:    {result.mae_cr:.2f} Cr")
        print(f"   Test R²:     {result.r2:.3f}")
        print(f"   Model:       {result.model_path}")
        print(f"   Metrics:     {METRICS_PATH}")
        print(f"   Best params: {result.best_params}")
        print("\nCheck ML in API (start server first):")
        print("   cd api && python app.py")
        print("   curl http://localhost:8000/api/health")
        print("   curl http://localhost:8000/api/ml/status")
    except Exception as e:
        print(f"❌ Training failed: {e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
