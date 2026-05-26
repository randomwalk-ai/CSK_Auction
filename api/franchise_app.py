"""
DEPRECATED — use app.py on port 8000 instead.

This file remains only so old bookmarks/scripts fail with a clear message.
"""

raise SystemExit(
    "franchise_app.py is deprecated. Run: cd api && python app.py\n"
    "Valuation endpoints: http://localhost:8000/api/players/valuation/{name}\n"
    "Aliases still work: /api/v2/players/valuation/{name}"
)
