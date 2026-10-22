"""The download command's search options reach artist and release downloads."""

from importlib import import_module
from unittest.mock import Mock

import pytest

from discogseek.cli.main import build_parser, main
from discogseek.services.library import LibraryReleaseService

ARTIST_RESULT = {"artist": "Target Artist", "dry_run": True, "queue_errors": [], "search_errors": []}


def test_search_options_default_to_reuse_and_track_searches():
    args = build_parser().parse_args(["download", "Target Artist"])
    assert args.refresh_searches is False
    assert args.search_missing_tracks is True

    args = build_parser().parse_args(["download", "Target Artist", "--refresh-searches", "--no-track-search"])
    assert args.refresh_searches is True
    assert args.search_missing_tracks is False


@pytest.mark.parametrize("flags,refresh,track_search", [
    ([], False, True),
    (["--refresh-searches"], True, True),
    (["--no-track-search"], False, False),
    (["--refresh-searches", "--no-track-search"], True, False),
])
def test_artist_download_passes_search_options(monkeypatch, capsys, flags, refresh, track_search):
    scraper = Mock(return_value=Mock(run=Mock(return_value=ARTIST_RESULT)))
    monkeypatch.setattr(import_module("discogseek.cli.main"), "SlskdArtistScraper", scraper)

    assert main(["download", "Target Artist", "--dry-run", "--quiet", *flags]) == 0

    options = scraper.call_args.kwargs
    assert options["refresh_searches"] is refresh
    assert options["search_missing_tracks"] is track_search
    capsys.readouterr()


@pytest.mark.parametrize("flags,use_existing", [
    ([], True), (["--refresh-searches"], False), (["--force-refresh"], False),
])
def test_release_download_passes_official_tracks_and_refresh(monkeypatch, capsys, flags, use_existing):
    found = {"title": "Track One", "status": "found", "disc_number": 1, "track_number": "1"}
    missing = {"title": "Track Two", "status": "missing", "disc_number": 1, "track_number": "2"}
    extra = {"title": "Track One (Demo)", "status": "found", "is_extra": True, "disc_number": 1, "track_number": "3"}
    audit = {"artist": "Target Artist", "title": "Three Track EP", "tracks": [found, missing, extra],
             "found_count": 1, "missing_count": 1}
    requests = []

    def download_missing_tracks(self, **kwargs):
        requests.append(kwargs)
        return {"queued_tracks": kwargs["missing_tracks"], "queued_files": [], "queue_errors": []}

    monkeypatch.setattr(LibraryReleaseService, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(LibraryReleaseService, "audit_named_release", lambda self, *a, **k: audit)
    monkeypatch.setattr(LibraryReleaseService, "download_missing_tracks", download_missing_tracks)

    assert main(["download", "Target Artist", "--release", "Three Track EP", "--quiet", *flags]) == 0

    assert len(requests) == 1
    assert requests[0]["missing_tracks"] == [missing]
    # As the browser does: the official tracklist without local extras.
    assert requests[0]["official_tracks"] == [found, missing]
    assert requests[0]["use_existing"] is use_existing
    capsys.readouterr()
