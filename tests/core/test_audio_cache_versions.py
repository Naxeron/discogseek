"""Audio cache rows record their extractor version, totals, and never store failed reads."""

import sqlite3

import pytest

from discogseek.core.audio import AudioMetadata
from discogseek.core.cache import UnifiedCacheManager


def stored(cache, path, **fields):
    cache.store_audio_metadata_batch([AudioMetadata(path=path, title="Song", track_number="1", **fields)])


@pytest.mark.parametrize("name, reread", [
    ("01 Song.m4a", True),     # MP4 track/disc atoms and freeform MBIDs were ignored
    ("01 Song.alac", True),
    ("01 Song.ape", True),     # APEv2 'Track' was ignored
    ("01 Song.wv", True),
    ("01 Song.flac", False),   # Vorbis and ID3 tags were read; no network-wide re-read
    ("01 Song.mp3", False),
])
def test_rows_from_the_first_extractor_are_reread_only_for_changed_formats(tmp_path, name, reread):
    path = tmp_path / name
    path.write_bytes(b"audio")
    cache = UnifiedCacheManager(tmp_path / "cache.db")
    stored(cache, path, artist="Artist", extractor_version=1, duration=180.0)

    cached = cache.get_audio_metadata(path)

    assert (cached is None) is reread
    if cached is not None:
        assert cached.extractor_version == 1


@pytest.mark.parametrize("fields", [
    {"artist": "Lone\x00Guest"},
    {"mb_artist_ids": {"artist-a\x00artist-b"}},  # stored as a JSON list
], ids=["text-column", "id-list-column"])
def test_first_extractor_rows_with_nul_joined_values_are_reread(tmp_path, fields):
    path = tmp_path / "01 Song.mp3"
    path.write_bytes(b"audio")
    cache = UnifiedCacheManager(tmp_path / "cache.db")
    stored(cache, path, extractor_version=1, duration=180.0, **fields)

    assert cache.get_audio_metadata(path) is None


def test_current_rows_round_trip_totals_and_version(tmp_path):
    path = tmp_path / "01 Song.m4a"
    path.write_bytes(b"audio")
    cache = UnifiedCacheManager(tmp_path / "cache.db")
    stored(cache, path, disc_number=1, total_discs=2, track_total=12)

    cached = UnifiedCacheManager(tmp_path / "cache.db").get_audio_metadata(path)

    assert (cached.disc_number, cached.total_discs, cached.track_total) == (1, 2, 12)
    assert cached.extractor_version >= 2


def test_failed_reads_are_not_cached(tmp_path):
    path = tmp_path / "01 Song.flac"
    path.write_bytes(b"audio")
    cache = UnifiedCacheManager(tmp_path / "cache.db")
    stored(cache, path, album="Some Folder", read_error=True)

    assert cache.get_audio_metadata(path) is None


def test_pre_version_databases_gain_the_new_columns(tmp_path):
    database = tmp_path / "legacy.db"
    path = tmp_path / "01 Song.m4a"
    path.write_bytes(b"audio")
    fingerprint = path.stat()
    with sqlite3.connect(database) as connection:
        connection.execute("""
            CREATE TABLE audio_cache (
                path TEXT PRIMARY KEY, mtime REAL, size_bytes INTEGER,
                title TEXT, artist TEXT, album_artist TEXT, album TEXT,
                track_number TEXT, disc_number INTEGER, year TEXT, genres TEXT, mb_track_ids TEXT,
                mb_rec_ids TEXT, mb_artist_ids TEXT, mb_release_ids TEXT,
                bitrate_kbps INTEGER, bit_depth INTEGER, sample_rate INTEGER,
                channels INTEGER, duration REAL, is_lossless INTEGER,
                format_label TEXT, quality_score INTEGER, cached_at REAL
            )
        """)
        connection.execute(
            "INSERT INTO audio_cache (path, mtime, size_bytes, title, disc_number) VALUES (?, ?, ?, ?, ?)",
            (str(path), fingerprint.st_mtime, fingerprint.st_size, "Song", 1),
        )

    cache = UnifiedCacheManager(database)

    assert cache.get_audio_metadata(path) is None
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(audio_cache)")}
    assert {"total_discs", "track_total", "extractor_version"} <= columns
