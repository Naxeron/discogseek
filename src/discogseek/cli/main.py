"""Commands for discography audits, library browsing, and slskd downloads."""

import argparse
import json
import logging
import math
import sys
from pathlib import Path
from typing import List, Optional

from discogseek.cli.progress import download_progress
from discogseek.config import Config
from discogseek.core.report import BaseReportExporter
from discogseek.services.auditor import AuditorService
from discogseek.services.library import LibraryReleaseService
from discogseek.services.soulseek import SlskdArtistScraper


def positive_seconds(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("timeout must be a positive number")
    return number


def match_ratio(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or not 0 < number <= 1:
        raise argparse.ArgumentTypeError("min-match must be greater than 0 and at most 1")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="discogseek", description="Audit discographies and queue missing music through slskd."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    audit = commands.add_parser("audit", help="Find missing tracks for an artist or release")
    download = commands.add_parser(
        "download", aliases=["artist", "soulseek", "slsk"],
        help="Search slskd and queue missing tracks for an artist or release",
    )
    browse = commands.add_parser("browse", help="Browse incomplete library releases in a two-pane terminal UI")
    browse.add_argument("--artist", help="Initial artist filter (editable with / in the browser)")
    browse.add_argument("-d", "--music-dir", "--library-dir", type=Path,
                        default=Config.DEFAULT_LIBRARY_DIR, help="Local music library")
    browse.add_argument("--force-refresh", action="store_true", help="Refresh MusicBrainz metadata")
    browse.add_argument("--verbose", action="store_true", help="Show service progress in the browser")
    for command in (audit, download):
        command.add_argument("artist", help="Artist name (or artist MBID for a whole discography)")
        command.add_argument("-d", "--music-dir", "--library-dir", type=Path,
                             default=Config.DEFAULT_LIBRARY_DIR, help="Local music library")
        release = command.add_mutually_exclusive_group()
        release.add_argument("--release", help="Limit to a release title")
        release.add_argument("--release-id", help="Limit to an exact MusicBrainz release MBID")
        command.add_argument("--force-refresh", action="store_true", help="Refresh MusicBrainz metadata")
        command.add_argument("--full-scan", action="store_true", help="Inspect all local tags for artist audits")
        command.add_argument("--json", dest="export_json", help="Write results to JSON (use - for stdout)")
        verbosity = command.add_mutually_exclusive_group()
        verbosity.add_argument("--verbose", action="store_true", help="Log progress to stderr (default for downloads)")
        if command is download:
            verbosity.add_argument("-q", "--quiet", action="store_true", help="Hide download progress; still show results and errors")
    display = audit.add_mutually_exclusive_group()
    display.add_argument("--missing-only", action="store_true", help="Show only missing tracks")
    display.add_argument("--found-only", action="store_true", help="Show only found tracks")
    audit.add_argument("--txt", dest="export_txt", help="Export missing tracks to plain text")
    audit.add_argument("--csv", dest="export_csv", help="Export audit to CSV")
    for command in (download, browse):
        command.add_argument("-f", "--format", default="flac", choices=["flac", "mp3-320"])
        command.add_argument("-t", "--timeout", type=positive_seconds, default=30.0,
                             help="Search timeout in seconds (default: 30)")
        command.add_argument("--dry-run", action="store_true", help="Show matches without queuing transfers")
    download.add_argument("--min-match", type=match_ratio, default=0.70,
                          help="Minimum album match ratio (default: 0.70)")
    return parser


def _json_default(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def _write_json(data, destination):
    text = json.dumps(data, ensure_ascii=False, indent=2, default=_json_default)
    if destination == "-":
        print(text)
    else:
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")


def _audit(args):
    if args.release or args.release_id:
        data = LibraryReleaseService().audit_named_release(
            args.artist, title=args.release, release_id=args.release_id,
            library_dir=args.music_dir, force_refresh=args.force_refresh,
        )
        tracks = data["tracks"]
        title = f"{data['artist']} - {data['title']}"
    else:
        auditor = AuditorService()
        catalog, found, missing = auditor.audit_artist(
            args.artist, music_dir=args.music_dir,
            full_scan=args.full_scan, force_refresh=args.force_refresh,
        )
        tracks = [dict(item["mb_track"], status=status,
                       path=item.get("local_track", {}).get("path"))
                  for status, items in (("found", found), ("missing", missing)) for item in items]
        data = {"artist": catalog.name, "mbid": catalog.mbid, "tracks": tracks,
                "found_count": len(found), "missing_count": len(missing)}
        title = catalog.name
    if args.export_csv:
        BaseReportExporter.export_csv(
            ["Status", "Track Title", "Artist", "Release", "Disc", "Track Number", "Path"],
            [[t["status"].upper(), t["title"], t.get("artist_credit", t.get("artist", title)),
              t.get("release_title", title), t.get("disc_number", 1),
              t.get("track_number", ""), t.get("path") or ""] for t in tracks],
            Path(args.export_csv),
        )
    if args.export_txt:
        BaseReportExporter.export_text(
            [f"{t.get('release_title', title)} | {t.get('disc_number', 1)}.{t.get('track_number', '')} | {t['title']}"
             for t in tracks if t["status"] == "missing"],
            Path(args.export_txt), header_title=f"Missing tracks: {title}",
        )
    if args.export_json:
        _write_json(data, args.export_json)
    if args.export_json != "-":
        print(f"{title}: {data['found_count']} found, {data['missing_count']} missing")
        for track in tracks:
            if args.missing_only and track["status"] != "missing":
                continue
            if args.found_only and track["status"] != "found":
                continue
            release = track.get("release_title") or title
            print(f"{track['status'].upper()} | {release} | "
                  f"{track.get('disc_number', 1)}.{track.get('track_number', '')} | {track['title']}")
    return 0


def _download(args):
    logger = logging.getLogger(__name__)
    logger.info("Starting %s for %s...", "dry run" if args.dry_run else "download", args.artist)
    if args.release or args.release_id:
        logger.info("Checking MusicBrainz and local/remote libraries for %s...", args.release or args.release_id)
        service = LibraryReleaseService()
        audit = service.audit_named_release(
            args.artist, title=args.release, release_id=args.release_id,
            library_dir=args.music_dir, force_refresh=args.force_refresh,
        )
        logger.info("Library: %s found, %s missing", audit["found_count"], audit["missing_count"])
        result = service.download_missing_tracks(
            artist=audit["artist"], release_title=audit["title"],
            missing_tracks=[t for t in audit["tracks"] if t["status"] == "missing"],
            preferred_format=args.format, search_timeout=args.timeout, dry_run=args.dry_run,
            on_progress=lambda done, total, message: logger.info(
                "%s [%s/%s tracks matched]", message, done, total,
            ),
        )
        matches = result.get("queued_files", [])
    else:
        result = SlskdArtistScraper(
            artist_query=args.artist, music_dir=args.music_dir, preferred_format=args.format,
            min_match_ratio=args.min_match, search_timeout=args.timeout, dry_run=args.dry_run,
            full_scan=args.full_scan, force_refresh=args.force_refresh,
        ).run()
        matches = [f for directory in result.get("queued_directories", [])
                   for f in directory["all_dir_files"]]
        matches += [t["file"] for t in result.get("verified_compilation_tracks", [])
                    + result.get("verified_standalone_tracks", [])]
    if args.export_json:
        _write_json(result, args.export_json)
    if args.export_json != "-":
        if args.dry_run:
            print(f"{len(matches)} matched files (dry run; no transfers queued)")
        else:
            print(f"{result.get('enqueued_count', result.get('queued_count', 0))} files queued through slskd")
        for file in matches:
            print(f"MATCH | {file['filename']}")
        for release in result.get("unresolved_releases", []):
            print(f"UNRESOLVED | {release.get('title', '')}")
        for track in result.get("unresolved_compilation_tracks", []) + result.get("unresolved_standalone_tracks", []):
            print(f"UNRESOLVED | {track.get('track', track.get('title', ''))}")
        if "total_missing" in result:
            print(f"{result['total_missing'] - result.get('resolved_count', 0)} tracks unresolved")
    for error in result.get("queue_errors", []):
        print(error.get("error", str(error)) if isinstance(error, dict) else error, file=sys.stderr)
    return 1 if result.get("queue_errors") else 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(message)s", stream=sys.stderr)
    try:
        if args.command == "browse":
            from discogseek.cli.browser import run_browser
            return run_browser(args)
        if args.command == "audit":
            return _audit(args)
        with download_progress(quiet=args.quiet):
            return _download(args)
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
