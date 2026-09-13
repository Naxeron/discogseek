"""
Unit tests for CLI parsing and subcommands.
"""

import pytest
from musicscraper.cli.main import build_parser


def test_cli_parser_commands():
    parser = build_parser()

    # Audit subcommand
    args = parser.parse_args(["audit", "Massive Attack", "--missing-only"])
    assert args.command == "audit"
    assert args.artist == "Massive Attack"
    assert args.missing_only is True

    # Soulseek subcommand
    args = parser.parse_args(["soulseek", "Aphex Twin", "--dry-run", "-f", "flac"])
    assert args.command == "soulseek"
    assert args.artist == "Aphex Twin"
    assert args.dry_run is True
    assert args.format == "flac"

    # Artist subcommand
    args = parser.parse_args(["artist", "Boards of Canada", "--dry-run", "-f", "flac"])
    assert args.command == "artist"
    assert args.artist == "Boards of Canada"
    assert args.dry_run is True
    assert args.format == "flac"

    # Web subcommand
    args = parser.parse_args(["web", "--port", "9090", "--host", "0.0.0.0"])
    assert args.command == "web"
    assert args.port == 9090
    assert args.host == "0.0.0.0"
