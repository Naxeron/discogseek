"""Persistent audio metadata cache storage and retrieval."""

import os
import sqlite3

import pytest

from discogseek.core.audio import AudioMetadata
from discogseek.core.cache import UnifiedCacheManager


def test_batch_cache_storage_and_prefetch(tmp_path):
    """Verifies SQLite cache batch write and bulk pre-fetch operations."""
    cache_db = tmp_path / "batch_cache.db"
    cache = UnifiedCacheManager(db_path=cache_db)

    files = [tmp_path / f"track_{i:03d}.flac" for i in range(20)]
    for f in files:
        f.write_text("data")

    metas = [
        AudioMetadata(
            path=f,
            title=f"Title {i}",
            artist="Batch Artist",
            album="Batch Album",
            track_number=str(i + 1),
            is_lossless=True,
            quality_score=95
        )
        for i, f in enumerate(files)
    ]

    cache.store_audio_metadata_batch(metas)
    fetched = cache.get_audio_metadata_batch(files)
    assert set(fetched) == set(files)
    assert all(meta.artist == "Batch Artist" for meta in fetched.values())


def test_legacy_cache_migrates_and_refreshes_missing_disc_metadata(tmp_path):
    database = tmp_path / "legacy.db"
    path = tmp_path / "01 Song.flac"
    path.write_bytes(b"audio")
    fingerprint = path.stat()
    with sqlite3.connect(database) as connection:
        connection.execute("""
            CREATE TABLE audio_cache (
                path TEXT PRIMARY KEY, mtime REAL, size_bytes INTEGER,
                title TEXT, artist TEXT, album_artist TEXT, album TEXT,
                track_number TEXT, year TEXT, genres TEXT, mb_track_ids TEXT,
                mb_rec_ids TEXT, mb_artist_ids TEXT, mb_release_ids TEXT,
                bitrate_kbps INTEGER, bit_depth INTEGER, sample_rate INTEGER,
                channels INTEGER, duration REAL, is_lossless INTEGER,
                format_label TEXT, quality_score INTEGER, cached_at REAL
            )
        """)
        connection.execute(
            "INSERT INTO audio_cache (path, mtime, size_bytes, title, album_artist) VALUES (?, ?, ?, ?, ?)",
            (str(path), fingerprint.st_mtime, fingerprint.st_size, "Song", "Various Artists"),
        )

    cache = UnifiedCacheManager(database)

    # The old row cannot prove which disc its track belongs to and must refresh.
    assert cache.get_audio_metadata(path) is None
    with cache._get_conn() as connection:
        assert connection.execute("SELECT album_artist FROM audio_cache").fetchone()[0] == "Various Artists"
    cache.store_audio_metadata(AudioMetadata(
        path=path, title="Song", album_artist="Various Artists", track_number="1", disc_number=2,
    ))
    reopened = UnifiedCacheManager(database)
    restored = reopened.get_audio_metadata(path)
    assert restored.disc_number == 2
    assert restored.album_artist == "Various Artists"


def test_batch_lookup_preserves_file_fingerprints_and_skips_bad_entries(tmp_path):
    cache = UnifiedCacheManager(tmp_path / "cache.db")
    names = ("unchanged", "retagged", "resized", "deleted", "corrupt", "new")
    paths = {name: tmp_path / f"{name}.flac" for name in names}
    for path in paths.values():
        path.write_bytes(b"audio")
    cache.store_audio_metadata_batch([
        AudioMetadata(path=path, title=name) for name, path in paths.items() if name != "new"
    ])
    stat = paths["retagged"].stat()
    os.utime(paths["retagged"], (stat.st_atime, stat.st_mtime + 10))
    stat = paths["resized"].stat()
    paths["resized"].write_bytes(b"longer audio")
    os.utime(paths["resized"], (stat.st_atime, stat.st_mtime))
    paths["deleted"].unlink()
    with cache._get_conn() as connection:
        connection.execute("UPDATE audio_cache SET genres = 'invalid json' WHERE path = ?", (str(paths["corrupt"]),))

    fetched = cache.get_audio_metadata_batch(list(paths.values()))
    assert set(fetched) == {paths["unchanged"]}
    assert fetched[paths["unchanged"]].title == "unchanged"


def test_batch_cache_uses_bounded_queries_and_transactions(tmp_path, monkeypatch):
    cache = UnifiedCacheManager(tmp_path / "cache.db")
    paths = [tmp_path / f"{number}.flac" for number in range(1001)]
    for path in paths:
        path.write_bytes(b"audio")
    statements = []
    connections = []
    connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    cache.store_audio_metadata_batch([AudioMetadata(path=path, title=path.stem) for path in paths])
    assert len(connections) == 3
    assert sum(statement.startswith("COMMIT") for statement in statements) == 3
    assert_connections_closed(connections)

    connections.clear()
    statements.clear()
    fetched = cache.get_audio_metadata_batch(paths)
    assert len(fetched) == len(paths)
    assert len(connections) == 1
    assert sum(statement.startswith("SELECT") for statement in statements) == 3
    assert_connections_closed(connections)


@pytest.fixture
def tracked_connections(monkeypatch):
    """Keep connections alive so garbage collection cannot hide a leaked handle."""
    original_connect = sqlite3.connect
    connections = []

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    yield connections
    for connection in connections:
        connection.close()


def assert_connections_closed(connections):
    assert connections
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_cache_closes_connections_on_writes_hits_and_early_returns(tmp_path, tracked_connections):
    cache = UnifiedCacheManager(tmp_path / "cache.db")
    path = tmp_path / "track.flac"
    path.write_bytes(b"audio")
    assert cache.get_audio_metadata(path) is None
    cache.store_audio_metadata(AudioMetadata(path=path, title="Track"))
    assert cache.get_audio_metadata(path).title == "Track"
    cache.store_api_cache("test", "hit", {"value": 1})
    cache.store_api_cache("test", "expired", {}, ttl_seconds=-1)
    assert cache.get_api_cache("test", "hit") == {"value": 1}
    assert cache.get_api_cache("test", "miss") is None
    assert cache.get_api_cache("test", "expired") is None
    assert cache.clear_expired() == 1
    assert_connections_closed(tracked_connections)


def test_cache_closes_connections_when_database_operations_fail(tmp_path, tracked_connections):
    cache = UnifiedCacheManager(tmp_path / "cache.db")
    with cache._get_conn() as connection:
        connection.execute("DROP TABLE audio_cache")
        connection.execute("DROP TABLE api_cache")
    path = tmp_path / "track.flac"
    path.write_bytes(b"audio")
    cache.store_audio_metadata(AudioMetadata(path=path, title="Track"))
    assert cache.get_audio_metadata(path) is None
    cache.store_api_cache("test", "key", {})
    assert cache.get_api_cache("test", "key") is None
    assert cache.clear_expired() == 0
    assert_connections_closed(tracked_connections)


def test_connection_context_commits_or_rolls_back_before_closing(tmp_path, tracked_connections):
    cache = UnifiedCacheManager(tmp_path / "cache.db")
    with cache._get_conn() as connection:
        connection.execute("INSERT INTO api_cache (namespace, cache_key, data_json) VALUES ('test', 'committed', '1')")
    with pytest.raises(RuntimeError, match="abort"):
        with cache._get_conn() as connection:
            connection.execute("INSERT INTO api_cache (namespace, cache_key, data_json) VALUES ('test', 'rolled-back', '2')")
            raise RuntimeError("abort")
    assert cache.get_api_cache("test", "committed") == 1
    assert cache.get_api_cache("test", "rolled-back") is None
    assert_connections_closed(tracked_connections)
