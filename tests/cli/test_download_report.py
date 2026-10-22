"""The download command's report shows partial results: every match, each track left missing, failed searches."""

import json
from importlib import import_module
from unittest.mock import Mock

from discogseek.cli.main import main
from discogseek.clients.slskd import SlskdClient
from tests.cli.test_commands import workflow  # noqa: F401  (fixture)
from tests.helpers.artist_mode import FakeArtistClient, raw_catalog, release, response, run_scraper, scraper_for

SIGNALS = [f"Signal {word}" for word in (
    "Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel", "India", "Juliet",
)]


def numbered(titles, start=1):
    return [f"{number:02d} {title}" for number, title in enumerate(titles, start)]


def report(monkeypatch, capsys, result, *args):
    """Run the download command on a prepared artist result."""
    monkeypatch.setattr(import_module("discogseek.cli.main"), "SlskdArtistScraper",
                        Mock(return_value=Mock(run=Mock(return_value=result))))
    code = main(["download", "Target Artist", "--quiet", *args])
    return code, capsys.readouterr()


def test_artist_dry_run_lists_each_folders_share_and_loose_files(monkeypatch, capsys):
    raw = raw_catalog([release("Phonetic Dreams", SIGNALS)])
    flac = [f"@@f\\Music\\Target Artist - Phonetic Dreams [FLAC]\\{name}.flac" for name in numbered(SIGNALS[:8])]
    mp3 = [f"@@m\\Music\\Target Artist - Phonetic Dreams\\{name}.mp3" for name in numbered(SIGNALS[:9])]
    loose = "@@l\\Music\\Target Artist\\Loose\\Target Artist - Signal Juliet.flac"
    client = FakeArtistClient(responses=[response("flacpeer", *flac), response("mp3peer", *mp3),
                                         response("loosepeer", loose)])
    result = run_scraper(scraper_for(raw, client, dry_run=True), monkeypatch, raw)

    code, output = report(monkeypatch, capsys, result, "--dry-run")

    assert code == 0
    assert client.calls == []
    # The best folder's eight tracks, the next folder's ninth, and the loose tenth.
    matched = sorted(line[len("MATCH | "):] for line in output.out.splitlines() if line.startswith("MATCH | "))
    assert matched == sorted(flac + [mp3[8], loose])
    assert "10 matched files (dry run; no transfers queued)" in output.out
    assert "UNRESOLVED" not in output.out


def test_artist_report_lists_each_missing_track_and_failed_search(monkeypatch, capsys):
    result = {
        "artist": "Target Artist", "dry_run": False, "enqueued_count": 8, "queued_files": [],
        "unresolved_releases": [{"title": "Phonetic Dreams"}, {"id": "va-1", "title": "Shared Night Signals"},
                                {"title": "Quiet Album"}],
        "unresolved_tracks": [
            {"release": "Phonetic Dreams", "title": "Signal India", "disc": 1, "position": "9",
             "reason": "search failed", "error": "slskd is down"},
            {"release": "Phonetic Dreams", "title": "Signal Juliet", "disc": None, "position": "10",
             "reason": "no matching file found"},
            {"release": "Shared Night Signals", "title": "Neon Memory", "disc": 1, "position": "A1",
             "reason": "could not be queued"},
        ],
        "search_errors": [{"query": "Night Signals", "error": "HTTP 409: search exists"}],
        "queue_errors": [],
    }

    code, output = report(monkeypatch, capsys, result)

    assert code == 0
    assert output.out.splitlines() == [
        "8 files queued through slskd",
        "UNRESOLVED | Quiet Album",
        "UNRESOLVED | Phonetic Dreams | 1.9 | Signal India (search failed)",
        "UNRESOLVED | Phonetic Dreams | 1.10 | Signal Juliet (no matching file found)",
        "UNRESOLVED | Shared Night Signals | 1.A1 | Neon Memory (could not be queued)",
        "3 tracks unresolved",
    ]
    assert output.err == "Search failed: Night Signals: HTTP 409: search exists\n"

    # JSON on stdout stays a single document; the failed search still goes to stderr.
    code, output = report(monkeypatch, capsys, result, "--json", "-")
    assert json.loads(output.out) == result
    assert output.err == "Search failed: Night Signals: HTTP 409: search exists\n"


def test_release_download_lists_the_tracks_it_could_not_find(workflow, monkeypatch, capsys):  # noqa: F811
    nothing = {"responses": []}
    monkeypatch.setattr(SlskdClient, "search", lambda *a, **k: nothing)
    monkeypatch.setattr(SlskdClient, "batch_search", lambda s, queries, **k: {q: nothing for q in queries})

    assert main(["download", "Target Artist", "--release", "Three Track EP", "--quiet"]) == 0

    output = capsys.readouterr()
    assert "UNRESOLVED | Three Track EP | 1.3 | Track Three (no matching file found)" in output.out
    assert "1 tracks unresolved" in output.out
    assert workflow == []
