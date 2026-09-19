"""Offline CLI audit and download workflows using local and remote libraries."""

import json
from pathlib import Path

import pytest

from discogseek.cli.main import build_parser, main
from discogseek.clients.musicbrainz import MusicBrainzClient
from discogseek.clients.navidrome import NavidromeScanner
from discogseek.clients.slskd import SlskdClient
from discogseek.config import Config
from discogseek.core.audio import AudioMetadata, AudioQualityAnalyzer
from discogseek.services.library import LibraryReleaseService


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    artist = "Target Artist"
    title = "Three Track EP"
    titles = ["Track One", "Track Two", "Track Three"]
    release = {
        "id": "release-1", "title": title,
        "artist-credit": [{"artist": {"id": "artist-1", "name": artist}}],
        "medium-list": [{"position": 1, "track-list": [
            {"id": f"track-{i}", "number": str(i), "title": track,
             "recording": {"id": f"recording-{i}", "title": track}}
            for i, track in enumerate(titles, 1)
        ]}],
    }
    catalog = {"artist": {"id": "artist-1", "name": artist}, "releases_artist": [release]}
    monkeypatch.setattr(MusicBrainzClient, "resolve_artist_mbid", lambda *a: ("artist-1", artist))
    monkeypatch.setattr(MusicBrainzClient, "fetch_full_discography", lambda *a, **k: catalog)
    monkeypatch.setattr(MusicBrainzClient, "get_release_by_id", lambda *a, **k: release)
    monkeypatch.setattr(MusicBrainzClient, "search_release", lambda *a, **k: [release])

    music = tmp_path / "music" / artist / title
    music.mkdir(parents=True)
    local = music / "01 Track One.flac"
    local.touch()
    monkeypatch.setattr(AudioQualityAnalyzer, "analyze_file", lambda p: AudioMetadata(
        path=Path(p), title=titles[0], artist=artist, album=title, track_number="1",
        mb_rec_ids={"recording-1"}, file_type="flac", is_lossless=True,
    ))
    monkeypatch.setattr(Config, "NAVIDROME_URL", "http://navidrome.invalid")
    monkeypatch.setattr(Config, "NAVIDROME_USER", "tester")
    monkeypatch.setattr(Config, "NAVIDROME_TOKEN", "test-password")

    def nav_request(self, endpoint, params):
        if endpoint == "search3":
            return {"searchResult3": {"song": [
                {"id": f"nav-{i}", "title": titles[i-1], "artist": artist,
                 "album": title, "track": i, "musicBrainzId": f"recording-{i}",
                 "path": f"{artist}/{title}/{i:02d} {titles[i-1]}.flac"}
                for i in (1, 2)
            ]}}
        return {}
    monkeypatch.setattr(NavidromeScanner, "_api_request", nav_request)
    files = [{"filename": f"Music\\{artist} - {title}\\{i:02d} {track}.flac", "size": i * 1000}
             for i, track in enumerate(titles, 1)]
    responses = {"responses": [{"username": "peer", "files": files, "uploadSpeed": 10000}]}
    monkeypatch.setattr(SlskdClient, "get_application", lambda s: {})
    monkeypatch.setattr(SlskdClient, "get_queued_filenames", lambda s: set())
    monkeypatch.setattr(SlskdClient, "get_queued_track_fingerprints", lambda s: {
        "base_filenames": set(), "clean_titles": set(), "full_paths": set(),
    })
    monkeypatch.setattr(SlskdClient, "search", lambda *a, **k: responses)
    monkeypatch.setattr(SlskdClient, "batch_search", lambda s, queries, **k: {q: responses for q in queries})
    monkeypatch.setattr(SlskdClient, "browse_directory", lambda *a, **k: files)
    queued = []
    monkeypatch.setattr(SlskdClient, "enqueue_download", lambda s, user, files: queued.extend(files))
    return queued

@pytest.mark.parametrize("command", ["download", "artist", "soulseek", "slsk"])
def test_download_aliases(command):
    args = build_parser().parse_args([command, "Artist", "--dry-run"])
    assert args.artist == "Artist"
    assert args.dry_run
    assert args.timeout == 30

@pytest.mark.parametrize("command", ["web", "gui"])
def test_removed_interfaces_are_not_commands(command):
    with pytest.raises(SystemExit):
        build_parser().parse_args([command])

@pytest.mark.parametrize("flag,value", [("--timeout", "0"), ("--timeout", "nan"),
                                       ("--min-match", "1.1"), ("--min-match", "-1")])
def test_invalid_download_arguments(flag, value):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["download", "Artist", flag, value])

@pytest.mark.parametrize("scope", [[], ["--release", "Three Track EP"], ["--release-id", "release-1"]])
def test_audit_local_and_navidrome(workflow, capsys, scope):
    assert main(["audit", "Target Artist", *scope, "--json", "-"]) == 0
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["found_count"] == 2
    assert report["missing_count"] == 1
    assert [t["title"] for t in report["tracks"] if t["status"] == "missing"] == ["Track Three"]
    assert workflow == []

@pytest.mark.parametrize("scope", [[], ["--release", "Three Track EP"]])
@pytest.mark.parametrize("dry_run", [False, True])
def test_download_only_missing_files(workflow, capsys, scope, dry_run):
    args = ["download", "Target Artist", *scope, "--json", "-"]
    if dry_run:
        args.append("--dry-run")
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is dry_run
    if dry_run:
        assert workflow == []
    else:
        assert len(workflow) == 1
        assert workflow[0]["filename"].endswith("03 Track Three.flac")

@pytest.mark.parametrize("scope", [[], ["--release", "Three Track EP"]])
def test_queue_failure_is_not_success(workflow, monkeypatch, capsys, scope):
    def fail(*a, **k):
        raise RuntimeError("slskd unavailable")
    monkeypatch.setattr(SlskdClient, "enqueue_download", fail)
    assert main(["download", "Target Artist", *scope]) == 1
    output = capsys.readouterr()
    assert "slskd unavailable" in output.err
    assert "1 files queued" not in output.out

def test_unknown_release_is_unverified(workflow, monkeypatch, capsys):
    monkeypatch.setattr(MusicBrainzClient, "get_release_by_id", lambda *a, **k: None)
    monkeypatch.setattr(MusicBrainzClient, "search_release", lambda *a, **k: [])
    assert main(["audit", "Target Artist", "--release", "Unknown"]) == 1
    assert "Release not found" in capsys.readouterr().err
    assert workflow == []

def test_artist_scan_uses_remote_library_without_local_folder(workflow, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(Config, "DEFAULT_LIBRARY_DIR", tmp_path / "absent")
    assert main(["audit", "Target Artist", "--json", "-"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["found_count"] == 2

def test_remote_failure_prevents_incorrect_downloads(workflow, monkeypatch, capsys):
    monkeypatch.setattr(NavidromeScanner, "test_connection", lambda s: False)
    assert main(["download", "Target Artist"]) == 1
    assert "Navidrome connection failed" in capsys.readouterr().err
    assert workflow == []

def test_plain_audit_exports(workflow, capsys, tmp_path):
    csv_path, txt_path = tmp_path / "audit.csv", tmp_path / "missing.txt"
    assert main(["audit", "Target Artist", "--missing-only", "--csv", str(csv_path), "--txt", str(txt_path)]) == 0
    output = capsys.readouterr().out
    assert "MISSING |" in output
    assert "FOUND |" not in output
    assert "\x1b" not in output
    assert "Track Three" in txt_path.read_text()
    assert "FOUND,Track One" in csv_path.read_text()

def test_unresolved_placeholders_never_search(workflow, monkeypatch):
    service = LibraryReleaseService()
    monkeypatch.setattr(service, "audit_release", lambda data: data)
    monkeypatch.setattr(service.slskd_client, "search", lambda **k: pytest.fail("must not search placeholders"))
    with pytest.raises(ValueError, match="unresolved track placeholders"):
        service.download_missing_tracks("Artist", "Release", [{"title": "Track 01 (Missing)"}])

def test_remote_scan_failure_prevents_incorrect_downloads(workflow, monkeypatch, capsys):
    monkeypatch.setattr(NavidromeScanner, "_api_request", lambda s, endpoint, params: {} if endpoint == "ping" else None)
    assert main(["download", "Target Artist"]) == 1
    assert "Navidrome scan failed" in capsys.readouterr().err
    assert workflow == []

def test_dry_run_release_reports_zero_queued(workflow, capsys):
    assert main(["download", "Target Artist", "--release", "Three Track EP", "--dry-run", "--json", "-"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["queued_count"] == 0
    assert result["matched_count"] == 1


@pytest.mark.parametrize("command", ["download", "artist", "soulseek", "slsk"])
def test_download_progress_is_visible_before_connecting(workflow, monkeypatch, capsys, command):
    def connect(client):
        output = capsys.readouterr()
        assert "Starting dry run for Target Artist" in output.err
        assert "Connecting to slskd" in output.err
        assert output.out == ""
        return {}

    monkeypatch.setattr(SlskdClient, "get_application", connect)
    assert main([command, "Target Artist", "--dry-run"]) == 0
    output = capsys.readouterr()
    assert "Scanning local and configured remote libraries" in output.err
    assert "Executing parallel Soulseek searches" in output.err
    assert "matched files (dry run" in output.out
    assert "\x1b" not in output.err
    assert workflow == []


def test_download_reports_search_counts(workflow, monkeypatch, capsys):
    def batch_search(client, queries, **kwargs):
        kwargs["on_progress"](1, len(queries), queries[0])
        assert "Soulseek searches: 1/" in capsys.readouterr().err
        return {}

    monkeypatch.setattr(SlskdClient, "batch_search", batch_search)
    assert main(["download", "Target Artist", "--dry-run"]) == 0


@pytest.mark.parametrize("scope", [[], ["--release", "Three Track EP"], ["--release-id", "release-1"]])
@pytest.mark.parametrize("quiet", [False, True])
def test_download_progress_preserves_json(workflow, capsys, scope, quiet):
    flags = ["--quiet"] if quiet else []
    assert main(["download", "Target Artist", *scope, "--dry-run", "--json", "-", *flags]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["dry_run"] is True
    if quiet:
        assert output.err == ""
    else:
        assert "Starting dry run for Target Artist" in output.err
        assert "[00:00]" in output.err
        if scope:
            assert "Release search 1/" in output.err
            assert "[1/1 tracks matched]" in output.err


def test_quiet_download_still_reports_errors(workflow, monkeypatch, capsys):
    def fail(*args):
        raise RuntimeError("slskd unavailable")

    monkeypatch.setattr(SlskdClient, "get_application", fail)
    assert main(["download", "Target Artist", "--quiet"]) == 1
    assert capsys.readouterr().err == "Error: slskd unavailable\n"
