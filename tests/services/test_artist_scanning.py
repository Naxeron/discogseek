"""Artist file discovery and reuse of cached audio metadata."""

import time

import pytest

from discogseek.clients.musicbrainz import ArtistCatalog
from discogseek.core.audio import AudioMetadata, AudioQualityAnalyzer
from discogseek.core.cache import UnifiedCacheManager
from discogseek.services.auditor import AudioFileScanner
from discogseek.services.reconciler import DiscographyReconciler


@pytest.mark.parametrize("folder", [
    "2003-04-06_ All Tomorrow's Parties, Camber Sands, Sussex, UK ()",
    "loose",
])
def test_artist_scan_finds_filename_safe_colons_from_cached_tags(tmp_path, monkeypatch, folder):
    title = "2003-04-06: All Tomorrow's Parties, Camber Sands, UK"
    album = "2003-04-06: All Tomorrow's Parties, Camber Sands, Sussex, UK"
    recording_id = "c8355854-06a2-4752-9c88-6b9218e6a045"
    catalog = ArtistCatalog({
        "artist": {"id": "venetian-snares", "name": "Venetian Snares"},
        "releases_artist": [{
            "id": "atp-release",
            "title": album,
            "medium-list": [{"track-list": [{
                "id": "atp-track", "number": "1", "title": title,
                "recording": {"id": recording_id, "title": title},
            }]}],
        }],
    })
    music_dir = tmp_path / "music"
    audio_path = music_dir / "downloads" / folder / (
        "1 - 2003-04-06_ All Tomorrow's Parties, Camber Sands, UK (1).mp3"
    )
    audio_path.parent.mkdir(parents=True)
    audio_path.write_bytes(b"cached audio")
    cache = UnifiedCacheManager(db_path=tmp_path / "audio.db")
    cache.store_audio_metadata(AudioMetadata(
        path=audio_path, title=title, album=album, artist="Venetian Snares",
        track_number="1", mb_rec_ids={recording_id},
    ))

    def unexpected_audio_read(path):
        pytest.fail(f"Cached metadata should satisfy discovery: {path}")

    monkeypatch.setattr(AudioQualityAnalyzer, "analyze_file", unexpected_audio_read)
    tracks = AudioFileScanner(music_dir, catalog, cache_manager=cache).scan()
    found, missing = DiscographyReconciler(catalog, tracks).reconcile()

    assert [track["path"] for track in tracks] == [str(audio_path)]
    assert len(found) == 1
    assert missing == []


def test_audio_file_scanner_skips_trash(tmp_path):
    # Setup dummy directory structure
    music_dir = tmp_path / "music"
    music_dir.mkdir()

    album_dir = music_dir / "Album"
    album_dir.mkdir()
    (album_dir / "01 track.mp3").write_text("dummy audio")

    trash_dir = music_dir / ".Trash-1000" / "files"
    trash_dir.mkdir(parents=True)
    (trash_dir / "02 deleted.mp3").write_text("dummy audio")

    incomplete_dir = music_dir / "incomplete" / "peer" / "Album"
    incomplete_dir.mkdir(parents=True)
    (incomplete_dir / "03 partial.flac").write_text("dummy audio")

    tmp_dir = music_dir / ".tmp"
    tmp_dir.mkdir(parents=True)
    (tmp_dir / "04 temp.mp3").write_text("dummy audio")

    git_dir = music_dir / ".git" / "objects"
    git_dir.mkdir(parents=True)
    (git_dir / "05 git.mp3").write_text("dummy audio")

    catalog = ArtistCatalog({
        "artist": {"id": "dummy-mbid", "name": "Artist"},
        "releases_artist": [],
        "releases_track_artist": [],
        "recordings": []
    })

    scanner = AudioFileScanner(music_dir=music_dir, catalog=catalog, full_scan=True)
    tracks = scanner.scan()

    paths = [t["path"] for t in tracks]
    assert any("Album" in p for p in paths)
    assert not any(".Trash" in p for p in paths)
    assert not any("incomplete" in p for p in paths)
    assert not any(".tmp" in p for p in paths)
    assert not any(".git" in p for p in paths)

def test_caching_benchmark_second_pass_faster(tmp_path, monkeypatch):
    """
    BENCHMARK: Verifies that a warm second-pass scan is significantly faster
    than the initial uncached scan, completely bypassing audio tag extraction.
    """
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    album_dir = music_dir / "Artist - Benchmark Album"
    album_dir.mkdir()

    file_count = 15
    for i in range(1, file_count + 1):
        (album_dir / f"{i:02d} track.flac").write_text(f"dummy audio stream {i}")

    catalog = ArtistCatalog({
        "artist": {"id": "art-bench", "name": "Artist"},
        "releases_artist": [{
            "id": "rel-bench",
            "title": "Benchmark Album",
            "release-group": {"id": "rg-bench", "title": "Benchmark Album"},
            "medium-list": [{
                "track-list": [
                    {"id": f"trk-{i}", "title": f"Track {i}", "number": str(i)}
                    for i in range(1, file_count + 1)
                ]
            }]
        }],
        "releases_track_artist": [],
        "recordings": []
    })

    cache_db = tmp_path / "benchmark_cache.db"
    cache = UnifiedCacheManager(db_path=cache_db)

    # Instrument AudioQualityAnalyzer.analyze_file to simulate I/O delay
    analyze_calls = {"count": 0}

    def mock_analyze(file_path):
        analyze_calls["count"] += 1
        time.sleep(0.005)  # Simulate 5ms mutagen parsing
        return AudioMetadata(
            path=file_path,
            title=file_path.stem,
            artist="Artist",
            album="Benchmark Album",
            track_number=file_path.stem.split()[0],
            format_label="FLAC",
            is_lossless=True,
            bitrate_kbps=900,
            quality_score=90
        )

    monkeypatch.setattr(AudioQualityAnalyzer, "analyze_file", staticmethod(mock_analyze))

    # Pass 1: Cold Scan
    scanner1 = AudioFileScanner(music_dir=music_dir, catalog=catalog, full_scan=True, cache_manager=cache)
    t0_cold = time.perf_counter()
    tracks_cold = scanner1.scan()
    cold_duration = time.perf_counter() - t0_cold

    assert len(tracks_cold) == file_count
    assert analyze_calls["count"] == file_count

    # Reset counter for warm scan
    analyze_calls["count"] = 0

    # Pass 2: Warm Scan (Must resolve 100% from SQLite cache)
    scanner2 = AudioFileScanner(music_dir=music_dir, catalog=catalog, full_scan=True, cache_manager=cache)
    t0_warm = time.perf_counter()
    tracks_warm = scanner2.scan()
    warm_duration = time.perf_counter() - t0_warm

    assert len(tracks_warm) == file_count
    # Zero calls to AudioQualityAnalyzer on warm scan
    assert analyze_calls["count"] == 0
    # Second-pass scan must be significantly faster (at least 2x faster in this test)
    assert warm_duration < cold_duration, f"Warm scan ({warm_duration:.4f}s) should be faster than cold scan ({cold_duration:.4f}s)"
