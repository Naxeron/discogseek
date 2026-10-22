"""Track inventories include loose songs without changing release grouping."""

from unittest.mock import Mock

from discogseek.core.audio import AudioMetadata, AudioQualityAnalyzer
from discogseek.core.cache import UnifiedCacheManager
from discogseek.services.library_scan import scan_library_releases, scan_library_tracks


def test_track_scan_keeps_loose_songs_and_reuses_metadata_cache(tmp_path, monkeypatch):
    music = tmp_path / "music"
    music.mkdir()
    path = music / "Artist - Loose song.flac"
    path.write_bytes(b"audio")
    cache = UnifiedCacheManager(tmp_path / "audio.db")
    metadata = AudioMetadata(path=path, artist="Artist", title="Loose song")
    analyze = Mock(return_value=metadata)
    monkeypatch.setattr(AudioQualityAnalyzer, "analyze_file", analyze)

    assert scan_library_tracks(cache, music) == [metadata]
    analyze.assert_called_once_with(path)
    analyze.reset_mock()

    assert scan_library_tracks(cache, music)[0].title == "Loose song"
    assert scan_library_releases(cache, music) == []
    analyze.assert_not_called()


def test_track_scan_accepts_missing_library(tmp_path):
    cache = UnifiedCacheManager(tmp_path / "audio.db")
    assert scan_library_tracks(cache, tmp_path / "missing") == []
