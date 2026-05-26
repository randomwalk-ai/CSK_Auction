#!/usr/bin/env python3
"""
Single CLI for IPL player portraits (Groq verify → fetch → SQLite + disk).

Usage (from auction-data-pipeline/):
  python3 scripts/player_portraits.py rebuild --confirm
  python3 scripts/player_portraits.py rebuild --confirm --delay 1.5
  python3 scripts/player_portraits.py update --force-initials --delay 1.5
  python3 scripts/player_portraits.py audit
  python3 scripts/player_portraits.py audit --clean-stale --fix
  python3 scripts/player_portraits.py diagnose "Virat Kohli"
  python3 scripts/seed_espn_portraits.py --apply --delay 1.2
  python3 scripts/seed_ipl_facecards.py --apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional

import httpx

ROOT = Path(__file__).resolve().parent.parent
API_DIR = ROOT / "api"
AUCTION_DB = ROOT / "auction_data.db"
PORTRAITS_DIR = ROOT / "data" / "player_portraits"

sys.path.insert(0, str(API_DIR))

from env_loader import dotenv_candidates, groq_api_key, load_project_dotenv  # noqa: E402

ENV_FILE, ENV_CANDIDATES = load_project_dotenv()

from cricketer_identity import resolve_cricketer_profile  # noqa: E402
from player_portrait_store import (  # noqa: E402
    CRICBUZZ_HEADERS,
    PHOTO_SOURCES,
    _connect,
    _cricbuzz_urls,
    _download_image,
    _equivalent_player_keys,
    _is_rejected_portrait,
    _rebuild_equivalent_keys,
    _load_manifest,
    _lookup_cricbuzz_id,
    _normalize_key,
    _resolve_display_name,
    _safe_file_stem,
    _save_manifest,
    _thesportsdb_thumb,
    _wiki_page_thumb,
    _wiki_thumb,
    export_db_portraits_to_disk,
    fetch_and_cache_portrait,
    portrait_cache_report,
    portrait_image_bytes,
    purge_rejected_portraits,
    warm_portrait_cache,
    wipe_portrait_cache_completely,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _print_groq_status() -> None:
    if groq_api_key():
        print("Groq identity verification: enabled")
        if os.getenv("GROQ_SKIP_FOR_POOL", "1") == "1":
            print("  GROQ_SKIP_FOR_POOL=1 — auction-pool players skip Groq (avoids 429s).\n")
        else:
            print("  GROQ_SKIP_FOR_POOL=0 — Groq runs for every player (slow; may 429).\n")
        if os.getenv("PORTRAIT_REQUIRE_GROQ") == "1":
            print("PORTRAIT_REQUIRE_GROQ=1 — pool-high players still get portraits without Groq.\n")
        return
    print("Groq identity verification: disabled\n")
    print("  Checked for .env in:")
    for p in ENV_CANDIDATES:
        print(f"    {'✓' if p.is_file() else '—'} {p}")
    if not ENV_FILE:
        example = ROOT / ".env.example"
        if example.is_file():
            print(f"\n  Create: cp {example} {ROOT / '.env'}")
        print("  Or set GROQ_API_KEY in CSK_2/.env (parent folder).\n")


def _clear_identity_cache() -> None:
    conn = _connect()
    conn.execute("DELETE FROM player_identity_cache")
    conn.commit()
    conn.close()
    print("Cleared player_identity_cache (Groq will re-verify)\n")


def _finish_warm(stats: dict) -> int:
    purged = purge_rejected_portraits()
    if purged["removed"]:
        print(f"Purged {purged['removed']} placeholder/bad portraits")

    print(json.dumps(stats, indent=2))
    report = stats.get("report") or portrait_cache_report()
    photos = int(report.get("local_photos", 0))
    total = int(stats.get("total", 0))
    print(f"\nDone: {photos} photo files on disk / {total} players targeted")
    print(f"Folder: {report.get('portrait_dir')}")
    print(f"Manifest: {report.get('manifest')}")
    print("Hard-refresh the dashboard (Cmd+Shift+R) after a full rebuild.")
    return 0 if photos >= total * 0.5 else 1


def _test_network(player: str = "Virat Kohli") -> bool:
    canonical = _resolve_display_name(player)
    conn = _connect()
    pid = _lookup_cricbuzz_id(conn, player) or _lookup_cricbuzz_id(conn, canonical)
    conn.close()
    if not pid:
        print(f"No Cricbuzz ID for {player} — cannot test CDN.")
        return False
    with httpx.Client(follow_redirects=True, trust_env=False) as client:
        for url in _cricbuzz_urls(pid):
            got = _download_image(client, url, headers=CRICBUZZ_HEADERS)
            if got and not _is_rejected_portrait(got[0], "cricbuzz"):
                print(f"Network OK: {len(got[0])} bytes from Cricbuzz ({player})")
                return True
    print("Network test failed — Cricbuzz CDN not reachable or rate-limited.")
    return False


# ---------------------------------------------------------------------------
# diagnose
# ---------------------------------------------------------------------------


def cmd_diagnose(args: argparse.Namespace) -> int:
    name = (args.player or "Ishan Kishan").strip()
    conn = _connect()
    source = "initials"

    try:
        with httpx.Client(follow_redirects=True, trust_env=False) as client:
            canonical = _resolve_display_name(name, conn=conn, client=client)
            pid = _lookup_cricbuzz_id(conn, name) or _lookup_cricbuzz_id(conn, canonical)

            profile = resolve_cricketer_profile(
                conn,
                name,
                resolve_display_name=lambda n: _resolve_display_name(n, conn=conn),
                lookup_cricbuzz_id=_lookup_cricbuzz_id,
                use_cache=not args.no_cache,
                force_groq=args.no_cache,
            )

            print(f"Player:     {name}")
            print(f"Canonical:  {canonical}")
            print(f"Cricbuzz ID:{pid or 'none'}")
            print(
                f"Verified:   {profile.is_cricketer} ({profile.verification_source}, "
                f"{profile.confidence}, groq={profile.groq_verified})"
            )
            if profile.canonical_name and profile.canonical_name != name:
                print(f"Profile:    {profile.canonical_name}")
            if profile.wikipedia_title:
                print(f"Wiki title: {profile.wikipedia_title}")
            if profile.source_note:
                print(f"Note:       {profile.source_note}")

            if pid:
                print("\nCricbuzz URLs:")
                for url in _cricbuzz_urls(pid):
                    got = _download_image(client, url, headers=CRICBUZZ_HEADERS)
                    if got:
                        data, _ct = got
                        rej = _is_rejected_portrait(data, "cricbuzz")
                        print(f"  OK  {len(data):>6}b rejected={rej}  {url}")
                    else:
                        print(f"  FAIL                      {url}")

            if profile.photo_eligible:
                search = profile.canonical_name or canonical
                print("\nTheSportsDB:")
                print(f"  {_thesportsdb_thumb(client, search, conn=conn) or 'no match'}")
                print("\nWikipedia:")
                if profile.wikipedia_title:
                    wurl = _wiki_page_thumb(
                        client,
                        profile.wikipedia_title,
                        display_name=search,
                        trust_title=profile.groq_verified or profile.confidence == "high",
                        conn=conn,
                    )
                    print(f"  title hit: {wurl or 'no match'}")
                print(f"  search:    {_wiki_thumb(client, search, conn=conn) or 'no match'}")
            else:
                print("\nSkipping TheSportsDB/Wikipedia — identity not eligible for photos.")

            data, ct, source = fetch_and_cache_portrait(name, conn=conn)
            print(f"\nCached as: {source} ({len(data)} bytes, {ct})")
    finally:
        conn.close()

    return 0 if source in PHOTO_SOURCES else 1


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


def _load_2026_players() -> tuple[list[dict], int]:
    if not AUCTION_DB.exists():
        sys.exit(f"DB not found: {AUCTION_DB}")
    conn = sqlite3.connect(AUCTION_DB)
    conn.row_factory = sqlite3.Row
    duplicate_rows = conn.execute(
        """
        SELECT COUNT(*) FROM auction_prices_full
        WHERE year = 2026 AND player_name IS NOT NULL AND TRIM(player_name) != ''
        """
    ).fetchone()[0]
    rows = conn.execute(
        """
        SELECT player_name AS name, MAX(NULLIF(TRIM(cricbuzz_player_id), '')) AS player_id
        FROM auction_prices_full
        WHERE year = 2026 AND player_name IS NOT NULL AND TRIM(player_name) != ''
        GROUP BY player_name ORDER BY player_name
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows], int(duplicate_rows)


def _audit_players(players: list[dict], manifest: dict, conn) -> dict:
    disk_stems: dict[str, Path] = {}
    if PORTRAITS_DIR.is_dir():
        for path in PORTRAITS_DIR.iterdir():
            if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".svg", ".webp"):
                disk_stems[path.stem.lower()] = path

    ok, missing, initials_only = [], [], []
    pool_keys = {_normalize_key(str(p["name"])) for p in players}
    manifest_files = {str(entry.get("file") or "") for entry in manifest.values()}

    for player in players:
        name = str(player["name"]).strip()
        player_key = _normalize_key(name)
        stem = _safe_file_stem(player_key)
        entry = manifest.get(player_key)

        if entry:
            filename = str(entry.get("file") or "")
            filepath = PORTRAITS_DIR / filename
            source = str(entry.get("source") or "")
            exists = filepath.is_file()
            if exists and source in PHOTO_SOURCES:
                ok.append({**player, "file": filename, "source": source})
            elif exists and (source == "initials" or filename.endswith(".svg")):
                initials_only.append({**player, "file": filename})
            elif exists:
                ok.append({**player, "file": filename, "source": source, "note": "unknown-source"})
            else:
                missing.append({**player, "manifest_entry": entry})
        elif stem in disk_stems:
            path = disk_stems[stem]
            if path.suffix.lower() == ".svg":
                initials_only.append({**player, "file": path.name, "note": "disk-only"})
            else:
                ok.append({**player, "file": path.name, "note": "disk-only"})
        else:
            missing.append(player)

    player_stems = {_safe_file_stem(_normalize_key(p["name"])) for p in players}
    stale_manifest = [
        {
            "player_key": key,
            "display_name": entry.get("display_name", key),
            "file": entry.get("file"),
            "source": entry.get("source"),
        }
        for key, entry in manifest.items()
        if key not in pool_keys
    ]
    extra_on_disk = [
        path
        for stem, path in disk_stems.items()
        if stem not in player_stems and path.name not in manifest_files
    ]

    by_hash: dict[str, list[dict]] = {}
    for key, entry in manifest.items():
        if key not in pool_keys or str(entry.get("source") or "") not in PHOTO_SOURCES:
            continue
        filepath = PORTRAITS_DIR / str(entry.get("file") or "")
        if not filepath.is_file() or filepath.suffix.lower() == ".svg":
            continue
        digest = hashlib.sha256(filepath.read_bytes()).hexdigest()
        by_hash.setdefault(digest, []).append(
            {
                "player_key": key,
                "display_name": entry.get("display_name", key),
                "file": entry.get("file"),
                "source": entry.get("source"),
            }
        )

    duplicate_photos = []
    for digest, entries in by_hash.items():
        keys = sorted({_normalize_key(str(e["player_key"])) for e in entries})
        if len(keys) <= 1:
            continue
        if all(_equivalent_player_keys(keys[0], other, conn=conn) for other in keys[1:]):
            continue
        duplicate_photos.append({"hash": digest, "players": entries})

    return {
        "ok": ok,
        "initials_only": initials_only,
        "missing": missing,
        "stale_manifest": stale_manifest,
        "extra_on_disk": extra_on_disk,
        "duplicate_photos": duplicate_photos,
    }


def _clean_stale(result: dict, manifest: dict, *, dry_run: bool = False) -> dict:
    removed: list[dict] = []
    conn = _connect()
    try:
        for entry in result["stale_manifest"]:
            key = str(entry["player_key"])
            filepath = PORTRAITS_DIR / str(entry.get("file") or "")
            removed.append(entry)
            if dry_run:
                continue
            manifest.pop(key, None)
            conn.execute("DELETE FROM player_portraits WHERE player_key = ?", (key,))
            if filepath.is_file():
                filepath.unlink()
        for path in result["extra_on_disk"]:
            removed.append({"player_key": None, "file": path.name, "source": "orphan-file"})
            if not dry_run:
                path.unlink(missing_ok=True)
        if not dry_run:
            conn.commit()
            _save_manifest(manifest)
            portrait_image_bytes.cache_clear()
    finally:
        conn.close()
    return {"removed": removed, "dry_run": dry_run}


def _print_audit_report(players: list[dict], result: dict, duplicate_rows: int) -> None:
    total = len(players)
    n_ok, n_ini, n_mis = len(result["ok"]), len(result["initials_only"]), len(result["missing"])
    n_stale, n_ext, n_dup = (
        len(result["stale_manifest"]),
        len(result["extra_on_disk"]),
        len(result["duplicate_photos"]),
    )
    pct = round(100 * n_ok / total, 1) if total else 0
    print(f"\n{'=' * 62}")
    print(f"  Player Portrait Audit — 2026 Auction Pool ({total} players)")
    if duplicate_rows > total:
        print(f"  (DB has {duplicate_rows} rows — {duplicate_rows - total} duplicate rows ignored)")
    print(f"{'=' * 62}")
    print(f"  Real photo      : {n_ok:>4}  ({pct}%)")
    print(f"  Initials SVG    : {n_ini:>4}")
    print(f"  Missing         : {n_mis:>4}")
    print(f"  Stale manifest  : {n_stale:>4}")
    print(f"  Orphan files    : {n_ext:>4}")
    print(f"  Duplicate photos: {n_dup:>4}")
    print(f"{'=' * 62}\n")
    if result["duplicate_photos"]:
        print("Duplicate photo groups (same image, different players):")
        for group in result["duplicate_photos"][:15]:
            names = ", ".join(str(p.get("display_name") or p["player_key"]) for p in group["players"])
            print(f"  {group['hash'][:12]}  {names}")


def cmd_audit(args: argparse.Namespace) -> int:
    players, duplicate_rows = _load_2026_players()
    manifest = _load_manifest()
    conn = _connect()
    try:
        _rebuild_equivalent_keys(conn)
        result = _audit_players(players, manifest, conn)
        clean_info = {"removed": []}

        if args.clean_stale:
            clean_info = _clean_stale(result, manifest, dry_run=args.dry_run)
            if not args.dry_run:
                manifest = _load_manifest()
                result = _audit_players(players, manifest, conn)
    finally:
        conn.close()

    if args.json:
        print(
            json.dumps(
                {
                    "total": len(players),
                    "ok": len(result["ok"]),
                    "initials_only": len(result["initials_only"]),
                    "missing": len(result["missing"]),
                    "duplicate_photos": len(result["duplicate_photos"]),
                    "cache_report": portrait_cache_report(),
                    "clean": clean_info,
                },
                indent=2,
            )
        )
    else:
        _print_audit_report(players, result, duplicate_rows)

    code = 0
    if args.fix:
        print("\nRunning update --force-initials …\n")
        code = cmd_update(
            argparse.Namespace(
                force_initials=True,
                refresh_all=False,
                export_only=False,
                refresh_identity=False,
                delay=1.5,
            )
        )
    elif result["missing"] or result["initials_only"] or result["duplicate_photos"]:
        code = 1
    return code


# ---------------------------------------------------------------------------
# update (incremental fetch)
# ---------------------------------------------------------------------------


def cmd_update(args: argparse.Namespace) -> int:
    if args.export_only:
        print(json.dumps({"export": export_db_portraits_to_disk(), "report": portrait_cache_report()}, indent=2))
        return 0

    purge = purge_rejected_portraits()
    if purge["removed"]:
        print(f"Purged {purge['removed']} placeholder/bad portraits")

    if args.force_initials:
        print("Re-fetching initials-only players (keeps existing photos) …\n")
        _print_groq_status()
    elif args.refresh_all:
        print("Re-fetching all players (use rebuild --confirm for full wipe first) …\n")

    if args.refresh_identity:
        if not groq_api_key():
            print("Warning: GROQ_API_KEY not set — identity cache cleared but Groq cannot re-verify.\n")
        _clear_identity_cache()

    stats = warm_portrait_cache(
        force_initials=args.force_initials,
        refresh_all=args.refresh_all,
        delay_s=args.delay,
        show_progress=True,
    )
    return _finish_warm(stats)


# ---------------------------------------------------------------------------
# rebuild (wipe + full refetch)
# ---------------------------------------------------------------------------


def cmd_rebuild(args: argparse.Namespace) -> int:
    if not args.confirm:
        print("This deletes ALL cached portraits and rebuilds from scratch.")
        print("Re-run with:  python3 scripts/player_portraits.py rebuild --confirm\n")
        return 1

    _print_groq_status()

    if not args.skip_test and not _test_network(args.test_player):
        return 1

    print("Wiping portrait DB, identity cache, and disk files …")
    print(json.dumps({"wiped": wipe_portrait_cache_completely()}, indent=2))
    print()

    print(f"Re-fetching all players (delay={args.delay}s) … expect ~45–90 min.\n")
    stats = warm_portrait_cache(refresh_all=True, delay_s=args.delay, show_progress=True)
    code = _finish_warm(stats)

    print("\n--- Post-rebuild audit ---")
    cmd_audit(argparse.Namespace(json=False, clean_stale=False, dry_run=False, fix=False))
    return code


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="IPL player portraits — single entry point",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_rebuild = sub.add_parser("rebuild", help="Wipe cache and refetch all players from scratch")
    p_rebuild.add_argument("--confirm", action="store_true", help="Required safety flag")
    p_rebuild.add_argument("--skip-test", action="store_true", help="Skip network test")
    p_rebuild.add_argument("--test-player", default="Virat Kohli", help="Player for network test")
    p_rebuild.add_argument("--delay", type=float, default=1.5, help="Seconds between fetches")
    p_rebuild.set_defaults(func=cmd_rebuild)

    p_update = sub.add_parser("update", help="Incremental fetch (default: safe)")
    p_update.add_argument("--force-initials", action="store_true", help="Retry initials only")
    p_update.add_argument("--refresh-all", action="store_true", help="Re-fetch every player (no wipe)")
    p_update.add_argument("--export-only", action="store_true", help="Export SQLite blobs to disk")
    p_update.add_argument("--refresh-identity", action="store_true", help="Clear Groq identity cache first")
    p_update.add_argument("--delay", type=float, default=0.5, help="Seconds between fetches")
    p_update.set_defaults(func=cmd_update)

    p_audit = sub.add_parser("audit", help="Coverage report for 2026 pool")
    p_audit.add_argument("--json", action="store_true")
    p_audit.add_argument("--clean-stale", action="store_true")
    p_audit.add_argument("--dry-run", action="store_true")
    p_audit.add_argument("--fix", action="store_true", help="Run update --force-initials after audit")
    p_audit.set_defaults(func=cmd_audit)

    p_diag = sub.add_parser("diagnose", help="Debug one player end-to-end")
    p_diag.add_argument("player", nargs="?", default="Ishan Kishan")
    p_diag.add_argument("--no-cache", action="store_true", help="Bypass identity cache")
    p_diag.set_defaults(func=cmd_diagnose)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
