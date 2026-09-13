"""
Unified CLI Entrypoint and Subcommand Router for MusicScraper.
"""

import sys
import argparse
from pathlib import Path
from typing import List, Optional

from musicscraper.config import Config
from musicscraper.core.report import console
from musicscraper.services.auditor import AuditorService
from musicscraper.services.soulseek import SlskdArtistScraper
from musicscraper.services.artist import ArtistDownloadOrchestrator


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="musicscraper",
        description="MusicScraper - Music library audits and Soulseek downloads"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # 1. Audit Subcommand
    audit_p = subparsers.add_parser("audit", help="Audit local library against MusicBrainz discography")
    audit_p.add_argument("artist", help="Artist name or MusicBrainz artist ID (MBID)")
    audit_p.add_argument("--music-dir", "-d", default=str(Config.DEFAULT_LIBRARY_DIR), help="Path to local music library")
    audit_p.add_argument("--full-scan", action="store_true", help="Inspect tags across entire library without targeted discovery")
    audit_p.add_argument("--force-refresh", action="store_true", help="Bypass MusicBrainz cache")
    audit_p.add_argument("--missing-only", action="store_true", help="Display only missing tracks")
    audit_p.add_argument("--found-only", action="store_true", help="Display only found tracks")
    audit_p.add_argument("--json", dest="export_json", help="Export audit results to JSON file")
    audit_p.add_argument("--txt", dest="export_txt", help="Export missing tracks to plain text file")
    audit_p.add_argument("--csv", dest="export_csv", help="Export full audit table to CSV")
    audit_p.add_argument("--bandcamp-links", dest="export_bc", help="Export artist Bandcamp links to text file")

    # 2. Soulseek Subcommand
    slsk_p = subparsers.add_parser("soulseek", aliases=["slsk"], help="Search Soulseek via slskd and download missing discography items")
    slsk_p.add_argument("artist", help="Target artist name")
    slsk_p.add_argument("--music-dir", "-d", default=str(Config.DEFAULT_LIBRARY_DIR), help="Path to local library for pre-scan")
    slsk_p.add_argument("-f", "--format", default="flac", choices=["flac", "mp3-320"], help="Preferred audio format (default: flac)")
    slsk_p.add_argument("--min-match", type=float, default=0.70, help="Minimum tracklist match ratio (default: 0.70)")
    slsk_p.add_argument("-t", "--timeout", type=float, default=25.0, help="Soulseek search timeout in seconds (default: 25.0)")
    slsk_p.add_argument("--dry-run", action="store_true", help="Scan and verify matches without queueing transfers")

    # 3. Artist Subcommand
    artist_p = subparsers.add_parser("artist", help="End-to-end artist downloader (MusicBrainz + Soulseek)")
    artist_p.add_argument("artist", help="Artist name to download")
    artist_p.add_argument("-o", "--output-dir", default=str(Config.DEFAULT_OUTPUT_DIR), help="Output directory")
    artist_p.add_argument("-d", "--library-dir", default=str(Config.DEFAULT_LIBRARY_DIR), help="Local library path for pre-scan")
    artist_p.add_argument("-f", "--format", default="flac", choices=["flac", "mp3-320"], help="Preferred audio format")
    artist_p.add_argument("-t", "--timeout", type=float, default=25.0, help="Soulseek search timeout in seconds (default: 25.0)")
    artist_p.add_argument("--no-soulseek", action="store_true", help="Disable Soulseek queueing")
    artist_p.add_argument("--dry-run", action="store_true", help="Preview downloads without downloading")

    # 4. Web GUI Subcommand
    web_p = subparsers.add_parser("web", aliases=["gui"], help="Launch MusicScraper Web GUI server")
    web_p.add_argument("--host", default="127.0.0.1", help="Host interface to bind (default: 127.0.0.1)")
    web_p.add_argument("-p", "--port", type=int, default=8080, help="Port to listen on (default: 8080)")
    web_p.add_argument("--open", action="store_true", help="Automatically open GUI in default web browser")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    # Route subcommands
    if args.command == "audit":
        auditor = AuditorService()
        catalog, found, missing = auditor.audit_artist(
            artist_query=args.artist,
            music_dir=Path(args.music_dir),
            full_scan=args.full_scan,
            force_refresh=args.force_refresh
        )
        auditor.render_report(
            catalog, found, missing,
            only_missing=args.missing_only,
            only_found=args.found_only
        )
        auditor.export_reports(
            catalog, found, missing,
            json_path=Path(args.export_json) if args.export_json else None,
            txt_path=Path(args.export_txt) if args.export_txt else None,
            csv_path=Path(args.export_csv) if args.export_csv else None,
            bandcamp_path=Path(args.export_bc) if args.export_bc else None
        )
        return 0

    elif args.command in ("soulseek", "slsk"):
        scraper = SlskdArtistScraper(
            artist_query=args.artist,
            music_dir=Path(args.music_dir) if args.music_dir else None,
            preferred_format=args.format,
            min_match_ratio=args.min_match,
            search_timeout=args.timeout,
            dry_run=args.dry_run
        )
        scraper.run()
        return 0

    elif args.command == "artist":
        orchestrator = ArtistDownloadOrchestrator(
            artist_query=args.artist,
            output_dir=Path(args.output_dir),
            library_dir=Path(args.library_dir),
            preferred_format=args.format,
            dry_run=args.dry_run,
            use_soulseek=not args.no_soulseek,
            search_timeout=args.timeout
        )
        orchestrator.run()
        return 0

    elif args.command in ("web", "gui"):
        from musicscraper.web.server import start_server
        import webbrowser
        httpd = start_server(host=args.host, port=args.port)
        url = f"http://{args.host}:{args.port}"
        console.print(f"[bold green]✔ MusicScraper Web GUI running at:[/bold green] [bold cyan]{url}[/bold cyan]")
        console.print("[dim]Press Ctrl+C to stop server.[/dim]")
        if args.open:
            webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            console.print("\n[yellow]Shutting down Web GUI server...[/yellow]")
            httpd.shutdown()
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
