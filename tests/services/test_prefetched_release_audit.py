"""Reuse full release metadata and preserve local multi-disc identities."""

from unittest.mock import MagicMock

import pytest

from discogseek.clients.musicbrainz import ArtistCatalog
from discogseek.core.audio import AudioMetadata, AudioQualityAnalyzer
from discogseek.core.cache import UnifiedCacheManager
from discogseek.services.auditor import AudioFileScanner
from discogseek.services.library import LibraryReleaseService


@pytest.mark.parametrize("force_refresh", [False, True])
def test_prefetched_release_audit_skips_musicbrainz_requests(force_refresh):
    client = MagicMock()
    client.get_release_by_id.side_effect = AssertionError("Unexpected release lookup")
    client.search_release.side_effect = AssertionError("Unexpected release search")
    service = LibraryReleaseService(mb_client=client)
    release = {
        "id": "compilation", "title": "Shared Album",
        "artist-credit": [{"name": "Various Artists"}],
        "medium-list": [{
            "position": "2", "track-list": [
                {"id": "track-target", "number": "1", "title": "Target Song",
                 "artist-credit": [{"name": "Target Artist"}]},
                {"id": "track-other", "number": "2", "title": "Other Song",
                 "artist-credit": [{"name": "Other Artist"}]},
            ],
        }],
    }

    result = service.audit_release({
        "mb_release_id": "compilation", "title": "Shared Album", "artist": "Target Artist",
        "tracks": [{"path": "/music/Shared Album/01 Target Song.flac", "title": "Target Song",
                    "track_number": "1", "disc_number": 2, "artist": "Target Artist"}],
    }, force_refresh=force_refresh, mb_release=release)

    assert result["is_audited"]
    assert result["found_count"] == 1
    assert result["missing_count"] == 1
    missing = next(track for track in result["tracks"] if track["status"] == "missing")
    assert (missing["disc_number"], missing["track_number"], missing["artist"]) == (2, "2", "Other Artist")
    client.get_release_by_id.assert_not_called()
    client.search_release.assert_not_called()


def test_artist_scan_preserves_disc_and_album_artist_on_fresh_and_cached_reads(tmp_path, monkeypatch):
    path = tmp_path / "music" / "Shared Album" / "01 Target Song.flac"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"audio")
    metadata = AudioMetadata(
        path=path, title="Target Song", artist="Target Artist", album="Shared Album",
        album_artist="Various Artists", track_number="1", disc_number=2,
    )
    analyze = MagicMock(return_value=metadata)
    monkeypatch.setattr(AudioQualityAnalyzer, "analyze_file", analyze)
    cache = UnifiedCacheManager(tmp_path / "audio.db")
    catalog = ArtistCatalog({"artist": {"id": "target", "name": "Target Artist"}})
    scanner = AudioFileScanner(tmp_path / "music", catalog, full_scan=True, cache_manager=cache)

    fresh_tracks = scanner.scan()
    cached_tracks = scanner.scan()

    assert analyze.call_count == 1
    assert fresh_tracks == cached_tracks
    assert fresh_tracks[0]["disc_number"] == 2
    assert fresh_tracks[0]["album_artist"] == "Various Artists"


@pytest.mark.parametrize("recording_tag", [set(), {"recording-other"}])
def test_repeated_compilation_titles_keep_their_local_position(recording_tag):
    service = LibraryReleaseService(mb_client=MagicMock())
    release = {
        "id": "compilation", "title": "Shared Album",
        "artist-credit": [{"name": "Various Artists"}],
        "medium-list": [{
            "position": "1", "track-list": [
                {"id": "track-target", "number": "1", "title": "Intro",
                 "recording": {"id": "recording-target", "title": "Intro"},
                 "artist-credit": [{"name": "Target Artist"}]},
                {"id": "track-other", "number": "2", "title": "Intro",
                 "recording": {"id": "recording-other", "title": "Intro"},
                 "artist-credit": [{"name": "Other Artist"}]},
            ],
        }],
    }

    result = service.audit_release({
        "artist": "Various Artists", "title": "Shared Album", "tracks": [{
            "path": "/music/Shared Album/02 Other Artist - Intro.flac",
            "title": "Intro", "artist": "Other Artist", "disc_number": 1,
            "track_number": "2", "mb_rec_ids": recording_tag,
        }],
    }, mb_release=release)

    assert [(track["track_number"], track["artist"], track["status"]) for track in result["tracks"]] == [
        ("1", "Target Artist", "missing"),
        ("2", "Other Artist", "found"),
    ]
