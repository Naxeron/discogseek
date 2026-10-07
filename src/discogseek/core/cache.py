"""
Unified SQLite caching layer for audio metadata, quality analysis, and external API responses.
"""

import json
import time
import sqlite3
import hashlib
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional, Any, Set, Iterator, Tuple

from discogseek.config import Config
from discogseek.core.audio import AudioMetadata


class UnifiedCacheManager:
    """Consolidated SQLite cache manager with thread-safe connection handling."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path or Config.AUDIO_CACHE_DB).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _get_conn(self) -> Iterator[sqlite3.Connection]:
        """Finish the transaction and close its handle without waiting for GC."""
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        try:
            conn.row_factory = sqlite3.Row
            # SQLite's connection context manager commits/rolls back but does
            # not close the connection. Scans open one for every cached file.
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Initializes tables for audio files, quality inspections, and API response caches."""
        with self._get_conn() as conn:
            # 1. Audio metadata & quality cache
            conn.execute("""
                CREATE TABLE IF NOT EXISTS audio_cache (
                    path TEXT PRIMARY KEY,
                    mtime REAL,
                    size_bytes INTEGER,
                    title TEXT,
                    artist TEXT,
                    album_artist TEXT,
                    album TEXT,
                    track_number TEXT,
                    disc_number INTEGER,
                    year TEXT,
                    genres TEXT,
                    mb_track_ids TEXT,
                    mb_rec_ids TEXT,
                    mb_artist_ids TEXT,
                    mb_release_ids TEXT,
                    bitrate_kbps INTEGER,
                    bit_depth INTEGER,
                    sample_rate INTEGER,
                    channels INTEGER,
                    duration REAL,
                    is_lossless INTEGER,
                    format_label TEXT,
                    quality_score INTEGER,
                    cached_at REAL
                )
            """)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(audio_cache)")}
            if "disc_number" not in columns:
                # Older cache rows did not retain disc tags. Leave their new
                # value NULL so the next scan refreshes metadata from the file.
                conn.execute("ALTER TABLE audio_cache ADD COLUMN disc_number INTEGER")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_audio_mtime ON audio_cache (path, mtime)")

            # 2. Generic Key-Value / API Cache with TTL
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_cache (
                    namespace TEXT,
                    cache_key TEXT,
                    data_json TEXT,
                    expires_at REAL,
                    created_at REAL,
                    PRIMARY KEY (namespace, cache_key)
                )
            """)
            conn.commit()

    # --- Audio Metadata & Quality Cache Methods ---

    def get_audio_metadata(self, file_path: Path) -> Optional[AudioMetadata]:
        """Retrieves cached AudioMetadata if file mtime and size match."""
        return self.get_audio_metadata_batch([file_path]).get(file_path)

    def get_audio_metadata_batch(self, file_paths: List[Path]) -> Dict[Path, AudioMetadata]:
        """Read fresh file fingerprints in chunks using one database connection."""
        cached = {}
        if not file_paths:
            return cached
        try:
            with self._get_conn() as conn:
                # Stay below SQLite's older 999-variable limit and avoid a
                # connection and SELECT for every track in a warm library scan.
                for start in range(0, len(file_paths), 500):
                    fingerprints = {}
                    for file_path in file_paths[start:start + 500]:
                        try:
                            st = file_path.stat()
                            canonical_path = str(file_path.resolve())
                            fingerprints.setdefault(canonical_path, []).append((file_path, st))
                        except OSError:
                            continue
                    if not fingerprints:
                        continue
                    placeholders = ",".join("?" for _ in fingerprints)
                    rows = conn.execute(
                        f"SELECT * FROM audio_cache WHERE path IN ({placeholders})",
                        tuple(fingerprints),
                    )
                    for row in rows:
                        for file_path, st in fingerprints[row["path"]]:
                            if (row["mtime"] != st.st_mtime or row["size_bytes"] != st.st_size
                                    or row["disc_number"] is None):
                                continue
                            try:
                                cached[file_path] = self._audio_metadata_from_row(file_path, row)
                            except (TypeError, ValueError):
                                # A bad cache entry must not discard the other
                                # valid hits or prevent a fresh metadata read.
                                continue
        except Exception:
            pass
        return cached

    @staticmethod
    def _audio_metadata_from_row(file_path: Path, row: sqlite3.Row) -> AudioMetadata:
        return AudioMetadata(
            path=file_path,
            file_type=file_path.suffix.lower(),
            title=row["title"] or "",
            artist=row["artist"] or "",
            album_artist=row["album_artist"] or "",
            album=row["album"] or "",
            track_number=row["track_number"] or "",
            disc_number=int(row["disc_number"] or 1),
            year=row["year"] or "",
            genres=json.loads(row["genres"]) if row["genres"] else [],
            mb_track_ids=set(json.loads(row["mb_track_ids"])) if row["mb_track_ids"] else set(),
            mb_rec_ids=set(json.loads(row["mb_rec_ids"])) if row["mb_rec_ids"] else set(),
            mb_artist_ids=set(json.loads(row["mb_artist_ids"])) if row["mb_artist_ids"] else set(),
            mb_release_ids=set(json.loads(row["mb_release_ids"])) if row["mb_release_ids"] else set(),
            bitrate_kbps=row["bitrate_kbps"] or 0,
            bit_depth=row["bit_depth"] or 0,
            sample_rate=row["sample_rate"] or 0,
            channels=row["channels"] or 2,
            duration=row["duration"] or 0.0,
            is_lossless=bool(row["is_lossless"]),
            format_label=row["format_label"] or "",
            quality_score=row["quality_score"] or 0
        )

    def store_audio_metadata(self, meta: AudioMetadata) -> None:
        """Stores AudioMetadata into the cache."""
        self.store_audio_metadata_batch([meta])

    def store_audio_metadata_batch(
        self, metadata: List[AudioMetadata],
        expected_fingerprints: Optional[Dict[Path, Tuple[float, int]]] = None,
    ) -> None:
        """Store metadata in bounded transactions, retaining per-file validity."""
        for start in range(0, len(metadata), 500):
            rows = []
            for meta in metadata[start:start + 500]:
                try:
                    st = meta.path.stat()
                    if (expected_fingerprints is not None
                            and expected_fingerprints.get(meta.path) != (st.st_mtime, st.st_size)):
                        continue
                    rows.append(self._audio_metadata_values(meta, st.st_mtime, st.st_size))
                except (OSError, TypeError, ValueError):
                    continue
            if not rows:
                continue
            self._store_audio_metadata_rows(rows)

    def _store_audio_metadata_rows(self, rows: List[tuple]) -> None:
        try:
            with self._get_conn() as conn:
                conn.executemany("""
                    INSERT OR REPLACE INTO audio_cache (
                        path, mtime, size_bytes, title, artist, album_artist, album,
                        track_number, disc_number, year, genres, mb_track_ids, mb_rec_ids, mb_artist_ids,
                        mb_release_ids, bitrate_kbps, bit_depth, sample_rate, channels,
                        duration, is_lossless, format_label, quality_score, cached_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, rows)
        except Exception:
            pass

    @staticmethod
    def _audio_metadata_values(meta: AudioMetadata, mtime: float, size: int) -> tuple:
        return (
            str(meta.path.resolve()),
            mtime,
            size,
            meta.title,
            meta.artist,
            meta.album_artist,
            meta.album,
            meta.track_number,
            meta.disc_number,
            meta.year,
            json.dumps(meta.genres),
            json.dumps(list(meta.mb_track_ids)),
            json.dumps(list(meta.mb_rec_ids)),
            json.dumps(list(meta.mb_artist_ids)),
            json.dumps(list(meta.mb_release_ids)),
            meta.bitrate_kbps,
            meta.bit_depth,
            meta.sample_rate,
            meta.channels,
            meta.duration,
            1 if meta.is_lossless else 0,
            meta.format_label,
            meta.quality_score,
            time.time()
        )

    # --- Generic API / Key-Value Cache Methods ---

    def get_api_cache(self, namespace: str, key: str) -> Optional[Any]:
        """Retrieves cached JSON data for namespace:key if not expired."""
        try:
            now = time.time()
            with self._get_conn() as conn:
                cur = conn.execute(
                    "SELECT data_json, expires_at FROM api_cache WHERE namespace = ? AND cache_key = ?",
                    (namespace, key)
                )
                row = cur.fetchone()
                if not row:
                    return None
                if row["expires_at"] and now > row["expires_at"]:
                    return None
                return json.loads(row["data_json"])
        except Exception:
            return None

    def store_api_cache(self, namespace: str, key: str, data: Any, ttl_seconds: Optional[float] = None) -> None:
        """Stores arbitrary JSON data into the cache with optional TTL."""
        try:
            now = time.time()
            expires_at = (now + ttl_seconds) if ttl_seconds else None
            data_json = json.dumps(data)
            with self._get_conn() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO api_cache (namespace, cache_key, data_json, expires_at, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (namespace, key, data_json, expires_at, now))
                conn.commit()
        except Exception:
            pass

    def clear_expired(self) -> int:
        """Removes expired entries from the API cache."""
        try:
            now = time.time()
            with self._get_conn() as conn:
                cur = conn.execute("DELETE FROM api_cache WHERE expires_at IS NOT NULL AND expires_at < ?", (now,))
                deleted = cur.rowcount
                conn.commit()
                return deleted
        except Exception:
            return 0
