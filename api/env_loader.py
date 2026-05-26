"""Load .env from auction-data-pipeline or parent CSK_2 folder."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional, Tuple

_LOADED: Optional[Path] = None


def dotenv_candidates() -> List[Path]:
    api_dir = Path(__file__).resolve().parent
    return [
        api_dir.parent / ".env",  # auction-data-pipeline/.env
        api_dir / ".env",  # api/.env
        api_dir.parent.parent / ".env",  # CSK_2/.env
    ]


def load_project_dotenv(*, override: bool = True) -> Tuple[Optional[Path], List[Path]]:
    """
    Load the first existing .env from standard locations.
    Returns (loaded_path, all_candidates_checked).
    """
    global _LOADED
    candidates = dotenv_candidates()
    try:
        from dotenv import load_dotenv
    except ImportError:
        return None, candidates

    for env_file in candidates:
        if env_file.is_file():
            load_dotenv(env_file, override=override)
            _LOADED = env_file
            return env_file, candidates
    return None, candidates


def loaded_env_path() -> Optional[Path]:
    return _LOADED


def groq_api_key() -> Optional[str]:
    if not _LOADED:
        load_project_dotenv()
    key = (os.getenv("GROQ_API_KEY") or "").strip()
    return key or None
