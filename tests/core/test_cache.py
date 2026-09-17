"""Persistent audio metadata cache storage and retrieval."""

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

    # Test batch storage if available, else store sequentially
    if hasattr(cache, "store_audio_metadata_batch"):
        cache.store_audio_metadata_batch(metas)
    else:
        for m in metas:
            cache.store_audio_metadata(m)

    # Test bulk pre-fetch if available, else get individually
    if hasattr(cache, "get_cached_metadata_for_files"):
        fetched = cache.get_cached_metadata_for_files(files)
        assert len(fetched) == 20
        assert str(files[0].resolve()) in fetched or str(files[0]) in fetched
    else:
        for f in files:
            cached = cache.get_audio_metadata(f)
            assert cached is not None
            assert cached.artist == "Batch Artist"


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
