"""
Library auditor service checking local library and Navidrome against MusicBrainz discographies.
"""

import os
import re
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
from concurrent.futures import ThreadPoolExecutor


from discogseek.config import Config
from discogseek.core.constants import (
    AUDIO_EXTENSIONS,
    GENERIC_OR_COMMON_WORDS,
    IGNORED_SCAN_DIR_NAMES,
    IGNORED_SCAN_DIR_PREFIXES,
)
from discogseek.core.text import normalize_text
from discogseek.core.cache import UnifiedCacheManager
from discogseek.clients.musicbrainz import MusicBrainzClient, ArtistCatalog
from discogseek.clients.navidrome import NavidromeScanner
from discogseek.services.reconciler import DiscographyReconciler


def is_distinct_track_title(title_norm: str) -> bool:
    """Determines if a normalized track title is distinct enough to safely match standalone filenames."""
    if not title_norm or len(title_norm) < 3:
        return False
    if title_norm in GENERIC_OR_COMMON_WORDS:
        return False
    words = title_norm.split()
    if len(words) >= 2 and len(title_norm) >= 4:
        return True
    if len(words) == 1 and len(title_norm) >= 4 and title_norm not in GENERIC_OR_COMMON_WORDS:
        return True
    return False


class AudioFileScanner:
    """Scans local music directory with fast 2-stage discovery and persistent SQLite caching."""

    def __init__(
        self,
        music_dir: Path,
        catalog: ArtistCatalog,
        full_scan: bool = False,
        threads: int = 24,
        cache_manager: Optional[UnifiedCacheManager] = None
    ):
        self.music_dir = Path(music_dir).resolve()
        self.catalog = catalog
        self.full_scan = full_scan
        self.threads = threads
        self.cache = cache_manager or UnifiedCacheManager()

    def scan(self) -> List[Dict[str, Any]]:
        """Discovers and extracts metadata from candidate audio files."""
        if not self.music_dir.exists():
            return []

        # 1. Compile Artist Alias Regex
        alias_norms = sorted({normalize_text(a) for a in self.catalog.aliases if normalize_text(a) and len(normalize_text(a)) >= 2}, key=len, reverse=True)
        alias_regex = re.compile(r"(?:\b|_)(?:" + "|".join(re.escape(a) for a in alias_norms) + r")(?:\b|_)", re.IGNORECASE) if alias_norms else None

        # 2. Compile Release Title Regex
        rel_norms_set = set()
        for rel in self.catalog.releases:
            norm = normalize_text(rel.get("title", ""))
            if norm and len(norm) >= 2 and norm not in GENERIC_OR_COMMON_WORDS:
                rel_norms_set.add(norm)
        for trk in self.catalog.tracks:
            for rel_t in trk.get("all_releases", set()):
                norm = normalize_text(rel_t)
                if norm and len(norm) >= 2 and norm not in GENERIC_OR_COMMON_WORDS:
                    rel_norms_set.add(norm)
        rel_norms = sorted(rel_norms_set, key=len, reverse=True)
        rel_regex = re.compile(r"(?:\b|_)(?:" + "|".join(re.escape(r) for r in rel_norms) + r")(?:\b|_)", re.IGNORECASE) if rel_norms else None

        # 3. Compile Distinct Track Title Regex
        trk_norms_set = {
            trk.get("norm_title", "")
            for trk in self.catalog.tracks
            if is_distinct_track_title(trk.get("norm_title", ""))
        }
        trk_norms = sorted(trk_norms_set, key=len, reverse=True)
        trk_regex = re.compile(r"(?:\b|_)(?:" + "|".join(re.escape(t) for t in trk_norms) + r")(?:\b|_)", re.IGNORECASE) if trk_norms else None

        candidate_paths: List[str] = []

        # Stage 1: Walk directory
        for root, dirs, files in os.walk(self.music_dir):
            # Exclude incomplete, staging, trash, sync-version, and VCS directories in-place
            dirs[:] = [
                d for d in dirs
                if not d.startswith(IGNORED_SCAN_DIR_PREFIXES)
                and d.lower() not in IGNORED_SCAN_DIR_NAMES
            ]

            try:
                rel_parts = Path(root).relative_to(self.music_dir).parts
                if any(p.lower() in IGNORED_SCAN_DIR_NAMES or p.startswith(IGNORED_SCAN_DIR_PREFIXES) for p in rel_parts):
                    continue
            except ValueError:
                pass

            norm_root = normalize_text(root)
            dir_matches = bool((alias_regex and alias_regex.search(norm_root)) or (rel_regex and rel_regex.search(norm_root)))

            audio_in_dir = [f for f in files if os.path.splitext(f)[1].lower() in AUDIO_EXTENSIONS]
            if not audio_in_dir:
                continue

            if self.full_scan or dir_matches:
                for f in audio_in_dir:
                    candidate_paths.append(os.path.join(root, f))
            else:
                matching_files = [
                    f for f in audio_in_dir
                    if (alias_regex and alias_regex.search(normalize_text(f)))
                    or (trk_regex and trk_regex.search(normalize_text(f)))
                    or (rel_regex and rel_regex.search(normalize_text(f)))
                ]
                if len(matching_files) >= 2 or (matching_files and len(matching_files) == len(audio_in_dir)):
                    for f in audio_in_dir:
                        candidate_paths.append(os.path.join(root, f))
                else:
                    for f in matching_files:
                        candidate_paths.append(os.path.join(root, f))

        # Stage 2: Cache Lookup + Extraction
        local_tracks: List[Dict[str, Any]] = []
        uncached_paths: List[Path] = []

        for p_str in candidate_paths:
            p = Path(p_str)
            cached_meta = self.cache.get_audio_metadata(p)
            if cached_meta:
                local_tracks.append({
                    "path": str(cached_meta.path),
                    "filename": cached_meta.path.name,
                    "title": cached_meta.title,
                    "norm_title": cached_meta.norm_title,
                    "album": cached_meta.album,
                    "norm_album": cached_meta.norm_album,
                    "track_number": cached_meta.track_number,
                    "artists": [cached_meta.artist] if cached_meta.artist else [],
                    "mb_track_ids": cached_meta.mb_track_ids,
                    "mb_rec_ids": cached_meta.mb_rec_ids,
                    "mb_artist_ids": cached_meta.mb_artist_ids,
                    "mb_release_ids": cached_meta.mb_release_ids,
                    "source": "local"
                })
            else:
                uncached_paths.append(p)

        if uncached_paths:
            from discogseek.core.audio import AudioQualityAnalyzer
            with ThreadPoolExecutor(max_workers=self.threads) as pool:
                for meta in pool.map(AudioQualityAnalyzer.analyze_file, uncached_paths):
                    if meta:
                        self.cache.store_audio_metadata(meta)
                        local_tracks.append({
                            "path": str(meta.path),
                            "filename": meta.path.name,
                            "title": meta.title,
                            "norm_title": meta.norm_title,
                            "album": meta.album,
                            "norm_album": meta.norm_album,
                            "track_number": meta.track_number,
                            "artists": [meta.artist] if meta.artist else [],
                            "mb_track_ids": meta.mb_track_ids,
                            "mb_rec_ids": meta.mb_rec_ids,
                            "mb_artist_ids": meta.mb_artist_ids,
                            "mb_release_ids": meta.mb_release_ids,
                            "source": "local"
                        })

        return local_tracks


class AuditorService:
    """Orchestrates discography retrieval, local/navidrome scanning and reconciliation."""

    def __init__(
        self,
        mb_client: Optional[MusicBrainzClient] = None,
        cache_manager: Optional[UnifiedCacheManager] = None
    ):
        self.mb_client = mb_client or MusicBrainzClient()
        self.cache = cache_manager or UnifiedCacheManager()

    def audit_artist(
        self,
        artist_query: str,
        music_dir: Optional[Path] = None,
        navidrome_url: Optional[str] = None,
        navidrome_user: Optional[str] = None,
        navidrome_password: Optional[str] = None,
        full_scan: bool = False,
        force_refresh: bool = False,
        threads: int = 24
    ) -> Tuple[ArtistCatalog, List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Runs complete audit for an artist query against local disk and/or Navidrome."""
        mbid, canonical_name = self.mb_client.resolve_artist_mbid(artist_query)
        discog_data = self.mb_client.fetch_full_discography(mbid, force_refresh=force_refresh)
        catalog = ArtistCatalog(discog_data)

        local_tracks = self.scan_library(
            catalog, music_dir, full_scan=full_scan, threads=threads,
            navidrome_url=navidrome_url, navidrome_user=navidrome_user,
            navidrome_password=navidrome_password,
        )

        # 3. Reconcile
        reconciler = DiscographyReconciler(catalog, local_tracks)
        found_items, missing_items = reconciler.reconcile()

        return catalog, found_items, missing_items

    def scan_library(
        self, catalog: ArtistCatalog, music_dir: Optional[Path] = None,
        full_scan: bool = False, threads: int = 24,
        navidrome_url: Optional[str] = None, navidrome_user: Optional[str] = None,
        navidrome_password: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Collect local and configured remote tracks for a single reconciliation."""
        tracks = AudioFileScanner(
            music_dir=Path(music_dir or Config.DEFAULT_LIBRARY_DIR), catalog=catalog,
            full_scan=full_scan, threads=threads, cache_manager=self.cache,
        ).scan()
        nav_url = navidrome_url or Config.NAVIDROME_URL
        nav_user = navidrome_user or Config.NAVIDROME_USER
        nav_pass = navidrome_password or Config.NAVIDROME_TOKEN
        if nav_url:
            if not nav_user or not nav_pass:
                raise ValueError("Navidrome requires a username and password")
            scanner = NavidromeScanner(nav_url, nav_user, nav_pass, catalog=catalog)
            if not scanner.test_connection():
                raise RuntimeError(f"Navidrome connection failed: {scanner.last_error}")
            tracks.extend(scanner.scan())
        return tracks
