"""
Navidrome / Subsonic REST API scanner for remote music libraries.
"""

import os
import json
import random
import string
import hashlib
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Set, Optional, Any


from discogseek.config import Config
from discogseek.core.constants import GENERIC_OR_COMMON_WORDS
from discogseek.core.constants import LOSSLESS_EXTENSIONS
from discogseek.core.release_metadata import is_various_artists, parse_disc_and_track_number
from discogseek.core.text import normalize_text
from discogseek.clients.musicbrainz import ArtistCatalog


class NavidromeScanner:
    """Queries Navidrome/Subsonic API for artist discography and indexed audio tracks."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        catalog: Optional[ArtistCatalog] = None,
        timeout: int = 15
    ):
        self.base_url = (base_url or Config.NAVIDROME_URL).rstrip("/")
        self.username = username or Config.NAVIDROME_USER or getattr(Config, "NAVIDROME_USERNAME", "")
        self.password = password or Config.NAVIDROME_TOKEN or getattr(Config, "NAVIDROME_PASSWORD", "")
        self.catalog = catalog
        self.timeout = timeout
        self.last_error: Optional[str] = None

    def test_connection(self) -> bool:
        """Pings the Navidrome/Subsonic server to verify connectivity and credentials."""
        self.last_error = None
        if not self.base_url:
            self.last_error = "Navidrome URL not configured"
            return False
        if not self.username:
            self.last_error = "Navidrome username not configured"
            return False
        res = self._api_request("ping", {})
        return res is not None

    def _api_request(self, endpoint: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        salt = "".join(random.choices(string.ascii_letters + string.digits, k=12))
        token = hashlib.md5((self.password + salt).encode("utf-8")).hexdigest()

        req_params = {
            "u": self.username,
            "t": token,
            "s": salt,
            "v": "1.16.1",
            "c": "discogseek",
            "f": "json"
        }
        req_params.update(params)

        url = f"{self.base_url}/rest/{endpoint}.view?{urllib.parse.urlencode(req_params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "discogseek/1.0"})

        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    sub_resp = data.get("subsonic-response", {})
                    if sub_resp.get("status") == "ok":
                        self.last_error = None
                        return sub_resp
                    err = sub_resp.get("error", {})
                    self.last_error = err.get("message") or f"Subsonic error code {err.get('code')}"
                    return None
            except Exception as e:
                self.last_error = str(e)
                if attempt == 0 and ("name resolution" in str(e).lower() or "timed out" in str(e).lower()):
                    import time
                    time.sleep(0.3)
                    continue
                break
        return None

    def _scan_request(self, endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
        response = self._api_request(endpoint, params)
        if response is None:
            raise RuntimeError(f"Navidrome scan failed ({endpoint}): {self.last_error}")
        return response

    def scan_library_releases(
        self,
        on_progress: Optional[Callable[[int, int, str], None]] = None,
        page_size: int = 500,
        selected_release: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Read every indexed album and its full tracklist, with no artist search cap.

        A failed page or album request aborts the inventory: an incomplete remote
        inventory must never be treated as evidence that a track is missing.
        """
        if not 1 <= page_size <= 500:
            raise ValueError("Navidrome album page size must be between 1 and 500")
        albums: Dict[str, Dict[str, Any]] = {}
        offset = 0
        while True:
            response = self._scan_request("getAlbumList2", {
                "type": "alphabeticalByName", "size": page_size, "offset": offset,
            })
            if "albumList2" not in response:
                raise RuntimeError("Navidrome scan failed: missing album list")
            page = response["albumList2"].get("album", [])
            if not page:
                break
            new_ids = 0
            for album in page:
                album_id = album.get("id")
                if not album_id:
                    raise RuntimeError("Navidrome scan failed: album has no ID")
                if album_id not in albums:
                    albums[album_id] = album
                    new_ids += 1
            if not new_ids:
                raise RuntimeError("Navidrome scan failed: album pagination made no progress")
            offset += len(page)
            if on_progress:
                on_progress(0, len(albums), f"Found {len(albums)} Navidrome albums...")
            if len(page) < page_size:
                break

        candidates = [
            (album_id, summary) for album_id, summary in albums.items()
            if selected_release is None or self._matches_selected_album(summary, selected_release, True)
        ]
        releases = []
        for index, (album_id, summary) in enumerate(candidates, 1):
            response = self._scan_request("getAlbum", {"id": album_id})
            if "album" not in response:
                raise RuntimeError(f"Navidrome scan failed: missing album {album_id}")
            album = dict(summary, **response["album"])
            if selected_release is not None and not self._matches_selected_album(album, selected_release):
                continue
            songs = album.get("song", [])
            if album.get("songCount", 0) > len(songs):
                raise RuntimeError(f"Navidrome scan failed: incomplete tracklist for {album_id}")
            title = album.get("name") or album.get("title") or "Unknown Album"
            artist = album.get("displayArtist") or album.get("artist") or "Unknown Artist"
            is_va = bool(album.get("isCompilation")) or is_various_artists(artist)
            if is_va:
                artist = "Various Artists"
            release_id = album.get("musicBrainzId") or None
            tracks = []
            seen_songs = set()
            for song in songs:
                song_id = song.get("id") or song.get("path")
                if song_id and song_id in seen_songs:
                    continue
                if song_id:
                    seen_songs.add(song_id)
                path = song.get("path") or ""
                filename = os.path.basename(path.replace("\\", "/"))
                disc, number, total = parse_disc_and_track_number(
                    song.get("track"), filename=filename,
                    meta_disc=song.get("discNumber", 1),
                )
                track_artist = song.get("displayArtist") or song.get("artist") or artist
                track_artists = [track_artist]
                for credit in song.get("artists", []):
                    if credit.get("name") and credit["name"] not in track_artists:
                        track_artists.append(credit["name"])
                suffix = (song.get("suffix") or os.path.splitext(filename)[1].lstrip(".")).lower()
                recording_id = song.get("musicBrainzId")
                tracks.append({
                    "source": "navidrome", "remote_id": song.get("id"),
                    "path": path, "filename": filename,
                    "title": song.get("title") or filename or "Unknown Track",
                    "artist": track_artist, "artists": track_artists,
                    "album": title, "album_artist": artist,
                    "norm_title": normalize_text(song.get("title") or filename),
                    "norm_album": normalize_text(title),
                    "disc_number": disc or 1, "track_number": str(number) if number is not None else "",
                    "track_num_int": number, "total_in_tag": total,
                    "duration": song.get("duration"), "format": suffix.upper(),
                    "bitrate": song.get("bitRate"),
                    "is_lossless": "." + suffix in LOSSLESS_EXTENSIONS,
                    "mb_rec_ids": {recording_id} if recording_id else set(),
                    "mb_track_ids": set(), "mb_artist_ids": set(),
                    "mb_release_ids": {release_id} if release_id else set(),
                    "status": "found",
                })
            releases.append({
                "id": "navidrome_" + str(album_id), "navidrome_id": album_id,
                "artist": artist, "album_artist": artist, "title": title,
                "is_va": is_va, "mb_release_id": release_id,
                "year": str(album.get("year") or ""), "source": "navidrome",
                "folder_path": "Navidrome", "full_path": "", "tracks": tracks,
                "formats": sorted({track["format"] for track in tracks if track["format"]}),
                "found_count": len(tracks), "missing_count": 0,
                "total_tracks_expected": len(tracks), "status": "unverified", "is_audited": False,
            })
            if on_progress:
                on_progress(index, len(candidates), f"Reading Navidrome: {artist} — {title}")
        return releases

    @staticmethod
    def _matches_selected_album(
        album: Dict[str, Any], selected: Dict[str, Any], allow_unknown_artist: bool = False,
    ) -> bool:
        """Choose refresh candidates without treating another tagged edition as this one."""
        target_id = selected.get("mb_release_id")
        album_id = album.get("musicBrainzId")
        if target_id and album_id:
            return target_id == album_id
        navidrome_ids = set(selected.get("navidrome_ids") or [])
        if selected.get("navidrome_id"):
            navidrome_ids.add(selected["navidrome_id"])
        if album.get("id") in navidrome_ids:
            return True
        pairs = {tuple(pair) for pair in selected.get("browser_alias_keys") or []}
        if selected.get("browser_name_key"):
            pairs.add(tuple(selected["browser_name_key"]))
        for title in (selected.get("title"), selected.get("mb_release_title")):
            for artist in (selected.get("artist"), selected.get("album_artist")):
                if title and artist:
                    if selected.get("is_va") or is_various_artists(artist):
                        artist = "Various Artists"
                    pairs.add((normalize_text(artist), normalize_text(title)))
        title = normalize_text(album.get("name") or album.get("title") or "")
        artist = album.get("displayArtist") or album.get("artist") or ""
        if album.get("isCompilation") or is_various_artists(artist):
            artist = "Various Artists"
        if not artist and allow_unknown_artist:
            # Album summaries may omit artist credits; resolve just the same-title
            # candidates and apply the stricter identity check to their details.
            return any(title == known_title for _, known_title in pairs)
        return (normalize_text(artist), title) in pairs

    def scan(self) -> List[Dict[str, Any]]:
        """Queries Navidrome Subsonic API for tracks matching artist aliases and releases."""
        if not self.catalog or not self.base_url:
            return []

        found_songs: Dict[str, Dict[str, Any]] = {}
        processed_queries: Set[str] = set()

        # 1. Search by artist aliases
        for a in self.catalog.aliases:
            clean_a = a.strip()
            if clean_a and len(clean_a) >= 2 and clean_a.lower() not in processed_queries:
                processed_queries.add(clean_a.lower())
                res = self._scan_request("search3", {
                    "query": clean_a,
                    "artistCount": 20,
                    "albumCount": 50,
                    "songCount": 500
                })
                if res:
                    for s in res.get("searchResult3", {}).get("song", []):
                        sid = s.get("id") or s.get("path")
                        if sid:
                            found_songs[sid] = s

        # 2. Check for exact MBID match
        if self.catalog.mbid:
            artists_res = self._scan_request("getArtists", {})
            if artists_res:
                for idx in artists_res.get("artists", {}).get("index", []):
                    for artist in idx.get("artist", []):
                        if artist.get("musicBrainzId") == self.catalog.mbid:
                            artist_id = artist.get("id")
                            if artist_id:
                                artist_detail = self._scan_request("getArtist", {"id": artist_id})
                                if artist_detail:
                                    for alb in artist_detail.get("artist", {}).get("album", []):
                                        alb_id = alb.get("id")
                                        if alb_id:
                                            alb_detail = self._scan_request("getAlbum", {"id": alb_id})
                                            if alb_detail:
                                                for s in alb_detail.get("album", {}).get("song", []):
                                                    sid = s.get("id") or s.get("path")
                                                    if sid:
                                                        found_songs[sid] = s

        # 3. Search primary releases
        for rel in self.catalog.releases:
            rel_title = rel.get("title", "").strip()
            norm_rel = normalize_text(rel_title)
            if norm_rel and len(norm_rel) >= 5 and norm_rel not in GENERIC_OR_COMMON_WORDS and norm_rel not in processed_queries:
                processed_queries.add(norm_rel)
                res = self._scan_request("search3", {
                    "query": rel_title,
                    "artistCount": 5,
                    "albumCount": 20,
                    "songCount": 200
                })
                if res:
                    for s in res.get("searchResult3", {}).get("song", []):
                        sid = s.get("id") or s.get("path")
                        if sid:
                            found_songs[sid] = s

        nav_tracks: List[Dict[str, Any]] = []
        for s in found_songs.values():
            title = s.get("title", "")
            album = s.get("album", "")
            track_num = str(s.get("track", ""))
            path = s.get("path", "")
            artists = [s.get("artist", "")]
            for a in s.get("artists", []):
                name = a.get("name", "")
                if name and name not in artists:
                    artists.append(name)

            mb_rec_ids: Set[str] = set()
            mbid_s = s.get("musicBrainzId", "")
            if mbid_s:
                mb_rec_ids.add(mbid_s)

            nav_tracks.append({
                "path": path,
                "filename": os.path.basename(path),
                "title": title or "",
                "norm_title": normalize_text(title),
                "album": album or "",
                "album_artist": s.get("albumArtist", ""),
                "norm_album": normalize_text(album),
                "track_number": track_num or "",
                "disc_number": s.get("discNumber", 1) or 1,
                "artists": artists,
                "mb_track_ids": set(),
                "mb_rec_ids": mb_rec_ids,
                "mb_artist_ids": set(),
                "mb_release_ids": set(),
                "source": "navidrome"
            })

        return nav_tracks
