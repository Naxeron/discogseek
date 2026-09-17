"""Public entry point for library scanning, auditing, and missing-track downloads.

The service owns shared clients and coordinates the workflows; each workflow lives
in its own module with explicit dependencies. Metadata helpers remain importable
here for compatibility with existing callers.
"""

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from discogseek.clients.musicbrainz import ArtistCatalog, MusicBrainzClient
from discogseek.clients.slskd import SlskdClient
from discogseek.clients.navidrome import NavidromeScanner
from discogseek.config import Config
from discogseek.core import release_metadata
from discogseek.core.cache import UnifiedCacheManager
from discogseek.core.text import normalize_text
from discogseek.services import library_audit, library_download, library_scan

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
        self._slskd_client = slskd_client
        self.cache = cache_manager or UnifiedCacheManager()

    @property
    def slskd_client(self) -> SlskdClient:
        """Authenticate with slskd only when a download actually needs it."""
        if self._slskd_client is None:
            self._slskd_client = SlskdClient()
        return self._slskd_client

    @slskd_client.setter
    def slskd_client(self, client: SlskdClient) -> None:
        self._slskd_client = client

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

    def audit_named_release(
        self, artist: str, title: Optional[str] = None,
        release_id: Optional[str] = None, library_dir: Optional[Path] = None,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """Audit one MusicBrainz release against local files and Navidrome."""
        reference = self.audit_release(
            {"artist": artist, "title": title or "", "mb_release_id": release_id, "tracks": []},
            force_refresh=force_refresh,
        )
        if not reference.get("is_audited"):
            raise ValueError("Release not found in MusicBrainz; supply an exact --release-id")

        title = reference.get("mb_release_title") or title or ""
        reference["title"] = title
        release_artist = reference.get("artist") or artist
        titles = {normalize_text(title)}
        artists = {normalize_text(artist), normalize_text(release_artist)}
        tracks = []
        for release in self.scan_library_releases(library_dir=library_dir):
            matches_id = release.get("mb_release_id") == reference.get("mb_release_id")
            matches_name = (normalize_text(release.get("title")) in titles
                            and normalize_text(release.get("artist")) in artists)
            if matches_id or matches_name:
                tracks.extend(t for t in release["tracks"] if t.get("path") or t.get("filename"))

        if Config.NAVIDROME_URL:
            if not Config.NAVIDROME_USER or not Config.NAVIDROME_TOKEN:
                raise ValueError("Navidrome requires a username and password")
            catalog = ArtistCatalog({"artist": {"id": "", "name": release_artist}})
            catalog.aliases.add(artist)
            catalog.releases = [{"title": title}]
            scanner = NavidromeScanner(catalog=catalog)
            if not scanner.test_connection():
                raise RuntimeError(f"Navidrome connection failed: {scanner.last_error}")
            for track in scanner.scan():
                if normalize_text(track.get("album")) not in titles:
                    continue
                if not reference.get("is_va") and not any(
                    normalize_text(name) in artists
                    for name in track.get("artists", []) + [track.get("album_artist", "")]
                ):
                    continue
                # The release reconciler also accepts remote tracks without a disk path.
                track["artist"] = ", ".join(track.get("artists", []))
                tracks.append(track)

        # A remote index can include the same file scanned locally. Preserve the
        # local copy and keep disc numbers in the key for multi-disc releases.
        unique = {}
        for track in tracks:
            disc, number, _ = release_metadata.parse_disc_and_track_number(
                track.get("track_number"), filename=track.get("filename"),
                meta_disc=track.get("disc_number"),
            )
            key = (disc, number, normalize_text(track.get("title")))
            if key not in unique:
                unique[key] = track
        reference["tracks"] = list(unique.values())
        return self.audit_release(reference)

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
        search_timeout: float = 30.0,
        dry_run: bool = False,
        on_progress: Optional[Callable[[int, int, str], None]] = None,
        search_scope: str = "release",
    ) -> Dict[str, Any]:
        """Find requested missing tracks, starting with a release or selected-track search."""
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
            search_scope=search_scope,
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
