"""Public entry point for library scanning, auditing, and missing-track downloads.

The service owns shared clients and coordinates the workflows; each workflow lives
in its own module with explicit dependencies. Metadata helpers remain importable
here for compatibility with existing callers.
"""

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from musicscraper.clients.musicbrainz import MusicBrainzClient
from musicscraper.clients.slskd import SlskdClient
from musicscraper.core import release_metadata
from musicscraper.core.cache import UnifiedCacheManager
from musicscraper.core.report import console
from musicscraper.services import library_audit, library_download, library_scan

# Preserve helper imports used by existing callers of the original service module.
parse_disc_and_track_number = release_metadata.parse_disc_and_track_number
_format_artist_credit = release_metadata.format_artist_credit
_is_va_string = release_metadata.is_various_artists
_is_va_directory = release_metadata.is_various_artists_directory
_is_numeric_track_item = library_audit.is_numeric_track_item


class LibraryReleaseService:
    """Coordinate library discovery, release reconciliation, and track downloads."""

    def __init__(
        self,
        mb_client: Optional[MusicBrainzClient] = None,
        slskd_client: Optional[SlskdClient] = None,
        cache_manager: Optional[UnifiedCacheManager] = None,
    ):
        self.mb_client = mb_client or MusicBrainzClient()
        self.slskd_client = slskd_client or SlskdClient()
        self.cache = cache_manager or UnifiedCacheManager()

    def scan_library_releases(
        self,
        library_dir: Optional[Path] = None,
        force_rescan: bool = False,
        threads: int = 16,
        on_progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> List[Dict[str, Any]]:
        """Discover local audio files and group them into releases."""
        return library_scan.scan_library_releases(
            cache=self.cache,
            library_dir=library_dir,
            force_rescan=force_rescan,
            threads=threads,
            on_progress=on_progress,
        )

    def audit_all_releases(
        self,
        library_dir: Optional[Path] = None,
        force_refresh: bool = False,
        max_workers: int = 4,
        on_progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> List[Dict[str, Any]]:
        """Audit every local release against MusicBrainz to find missing tracks."""
        releases = self.scan_library_releases(library_dir=library_dir, force_rescan=force_refresh)
        if not releases:
            return []

        audited_releases: List[Dict[str, Any]] = []
        total = len(releases)

        for idx, rel in enumerate(releases):
            if on_progress:
                on_progress(
                    idx + 1,
                    total,
                    f"Auditing MusicBrainz for '{rel.get('artist')} - {rel.get('title')}'...",
                )
            try:
                audited = self.audit_release(rel, force_refresh=force_refresh)
                audited_releases.append(audited)
            except Exception as e:
                console.print(f"[yellow]Warning: Error auditing '{rel.get('title')}': {e}[/yellow]")
                audited_releases.append(rel)

        return audited_releases

    def audit_release(
        self, release_data: Dict[str, Any], force_refresh: bool = False
    ) -> Dict[str, Any]:
        """Reconcile local files against MusicBrainz and resolve missing track titles."""
        return library_audit.audit_release(
            mb_client=self.mb_client,
            release_data=release_data,
            force_refresh=force_refresh,
        )

    def download_missing_tracks(
        self,
        artist: str,
        release_title: str,
        missing_tracks: List[Dict[str, Any]],
        preferred_format: str = "flac",
        search_timeout: float = 25.0,
        dry_run: bool = False,
        on_progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> Dict[str, Any]:
        """Find missing tracks using album searches followed by individual searches."""
        return library_download.download_missing_tracks(
            slskd_client=self.slskd_client,
            audit_release=self.audit_release,
            artist=artist,
            release_title=release_title,
            missing_tracks=missing_tracks,
            preferred_format=preferred_format,
            search_timeout=search_timeout,
            dry_run=dry_run,
            on_progress=on_progress,
        )

    def download_single_missing_track(
        self,
        artist: str,
        release_title: str,
        track_title: str,
        track_artist: Optional[str] = None,
        track_number: Optional[Union[int, str]] = None,
        preferred_format: str = "flac",
        search_timeout: float = 28.0,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Resolve a missing track's title, search Soulseek, and queue a match."""
        return library_download.download_single_missing_track(
            slskd_client=self.slskd_client,
            audit_release=self.audit_release,
            artist=artist,
            release_title=release_title,
            track_title=track_title,
            track_artist=track_artist,
            track_number=track_number,
            preferred_format=preferred_format,
            search_timeout=search_timeout,
            dry_run=dry_run,
        )
