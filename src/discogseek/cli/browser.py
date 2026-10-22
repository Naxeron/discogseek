"""Two-pane, keyboard-driven release browser using the standard-library terminal UI."""

import copy
import locale
import logging
import queue
import sys
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Dict

import requests as http_requests

from discogseek.clients.slskd import SlskdAPIError
from discogseek.core import transfers as transfer_history
from discogseek.core.peer_policy import PeerPolicy, peer_key
from discogseek.core.text import normalize_text
from discogseek.core.transfers import (
    WAITING_STATES, is_final_failure, is_stale_queued, local_download_exists, parse_timestamp,
    resolve_downloads_dir, retry_budget, retry_option, slskd_retry_budget, source_key,
    stale_description, terminal_failure, transfer_states,
)
from discogseek.services.library_browser import LibraryBrowserService


DOWNLOAD_POLL_INTERVAL = 3.0
DOWNLOAD_IMPORT_RETRY_INTERVAL = 15.0
# While every watched transfer still waits, check the library now and then:
# a track obtained elsewhere becomes FOUND even if its transfer never starts.
DOWNLOAD_QUEUED_REFRESH_INTERVAL = 300.0
# A finished file that has left slskd's downloads folder, and is still missing
# from the library this long after finishing, was not imported.
DOWNLOAD_IMPORT_GRACE = 600.0
# Failures searched again elsewhere. Aborted means the remote size changed,
# which excludes that file but says nothing about the peer.
DOWNLOAD_RECOVERY_STATES = {"TimedOut", "Errored", "Rejected", "Aborted"}
# slskd may still retry these, so they are stopped before switching sources.
DOWNLOAD_CANCEL_STATES = {"TimedOut", "Errored", "Rejected"}
DOWNLOAD_RECOVERY_LIMIT = 2
# A recovery search interrupted by a service outage is retried later.
DOWNLOAD_RECOVERY_BACKOFF = 60.0
DOWNLOAD_RECOVERY_BACKOFF_MAX = 900.0
DOWNLOAD_RECOVERY_TRANSIENT_LIMIT = 3
DOWNLOAD_OPTIONS_RETRY_INTERVAL = 300.0
# slskd 0.26 does not report when it will retry a timed-out or errored attempt
# (no nextAttemptAt); it waits at most retry.maxDelay between attempts. Such a
# failure counts as final once it has stayed unchanged for longer than that.
DOWNLOAD_RETRY_DELAY_DEFAULT = transfer_history.RETRY_DELAY_DEFAULT_SECONDS
DOWNLOAD_RETRY_MARGIN = 15.0


class _ScanStopped(Exception):
    """Stop read-only discovery at the next progress callback."""


def _is_transient_error(error):
    """Whether a job failed because a service was briefly unreachable."""
    if isinstance(error, (SlskdAPIError, http_requests.RequestException, ConnectionError, TimeoutError)):
        return True
    return isinstance(error, RuntimeError) and str(error).startswith(
        ("Navidrome connection failed", "Navidrome scan failed")
    )


def _retry_wait(options):
    """Seconds a failed attempt may wait for slskd's next retry."""
    max_delay = retry_option(options, "maxDelay")
    delay = max_delay / 1000 if max_delay is not None and max_delay >= 0 else DOWNLOAD_RETRY_DELAY_DEFAULT
    return delay + 2 * DOWNLOAD_POLL_INTERVAL + DOWNLOAD_RETRY_MARGIN


def _last_activity(file):
    moments = [parse_timestamp(file.get(field)) for field in ("requestedAt", "enqueuedAt", "startedAt", "endedAt")]
    return max((moment for moment in moments if moment is not None), default=None)


def _is_later_attempt(file, previous):
    """Whether file, not previous, is the current transfer of the same source.

    slskd keeps an ended record when the same file is queued again (a file
    downloaded earlier but never imported), and reuses one record for its own
    retries. A live transfer is the current one; otherwise the newer record is.
    """
    live, previous_live = ("Completed" not in transfer_states(record) for record in (file, previous))
    if live != previous_live:
        return live
    moment, previous_moment = _last_activity(file), _last_activity(previous)
    if moment is not None and previous_moment is not None and moment != previous_moment:
        return moment > previous_moment
    # Without timestamps, the later record wins unless it would hide a healthy one.
    return not (terminal_failure(file) and not terminal_failure(previous))


def _attempt_count(file):
    try:
        return int(file.get("attempts") or 0)
    except (TypeError, ValueError):
        return 0


def _duration(seconds):
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min"
    if seconds >= 60:
        return f"{seconds // 60} min"
    return f"{seconds} s"


def release_key(release):
    return release.get("mb_release_id") or release.get("id") or (
        normalize_text(release.get("artist", "")), normalize_text(release.get("title", ""))
    )


def equivalent_release_key(release):
    # Editions can have different release/track MBIDs for the same recordings.
    # Share their browser and download state only with a fully verified tracklist;
    # keep the actual edition ID in the snapshot for audits and targeted refreshes.
    artist = normalize_text(release.get("album_artist") or release.get("artist") or "")
    title = normalize_text(release.get("mb_release_title") or release.get("title") or "")
    tracks = release.get("tracks") or []
    if release.get("is_audited") and release.get("mb_release_id") and artist and title and tracks:
        positions = set()
        recordings = []
        for track in tracks:
            disc, number, recording = track_key(track)
            if (not track.get("mb_recording_id") or not disc.isdigit() or int(disc) < 1
                    or number in ("", "-", "0") or (disc, number) in positions):
                break
            positions.add((disc, number))
            recordings.append((disc, number, recording))
        else:
            return ("recordings", artist, title, tuple(sorted(recordings)))
    return release_key(release)


def album_key(release):
    """Group verified editions of one album without sharing their track state."""
    if release.get("is_audited") and release.get("mb_release_group_id"):
        return ("album", release["mb_release_group_id"])
    return equivalent_release_key(release)


def edition_label(release):
    media = release.get("mb_release_media") or []
    formats = {}
    for medium in media:
        label = medium.get("format") or "Unknown format"
        formats[label] = formats.get(label, 0) + 1
    parts = [f"{count}×{label}" if count > 1 else label for label, count in formats.items()]
    if media:
        parts.append(f"{sum(medium.get('track_count') or 0 for medium in media)} tracks")
    parts.extend(str(release[field]) for field in ("mb_release_date", "mb_release_country")
                 if release.get(field))
    if release.get("mb_release_disambiguation"):
        parts.append(release["mb_release_disambiguation"])
    parts.append(str(release.get("mb_release_id") or release_key(release))[:8])
    return " · ".join(parts)


def track_key(track):
    def number(value):
        try:
            return str(int(value))
        except (TypeError, ValueError):
            return str(value)

    return (number(track.get("disc_number") or 1), number(track.get("track_number") or ""),
            track.get("mb_recording_id") or track.get("mb_track_id") or track.get("title", ""))


class BrowserModel:
    """Keep selection, filters, and successful queue submissions independent of curses."""

    def __init__(self, artist_filter=""):
        self.releases: Dict[Any, Dict] = {}
        self.artist_filter = artist_filter
        self.show_unverified = False
        self.selected_key = None
        self.track_index = 0
        self.queued = {}
        self.matched = {}
        self.downloaded = {}
        # Queued tracks whose transfer waits too long; they can be searched again.
        self.stale = {}
        self.preferred_editions = {}
        self.selected_editions = {}
        self._history_releases = {}

    def _equivalent_editions(self):
        groups = {}
        for release in self.releases.values():
            groups.setdefault((album_key(release), equivalent_release_key(release)), []).append(release)
        rows = []
        for key, editions in groups.items():
            # Freshness determines whether equivalent tracklists are incomplete,
            # independently of which edition the user wants to inspect.
            preferred = self.preferred_editions.get(key[1])
            rows.append(min(editions, key=lambda r: (
                release_key(r) != preferred if preferred else False,
                r.get("missing_count", 0), release_key(r) != self.selected_key,
            )))
        return rows

    def editions(self, release):
        key = album_key(release)
        return sorted(
            [row for row in self.releases.values() if album_key(row) == key],
            key=lambda row: (row.get("missing_count", 0), str(release_key(row))),
        )

    def choose_edition(self, release):
        self.selected_editions[album_key(release)] = release_key(release)
        self.selected_key = release_key(release)
        self.track_index = 0

    def visible(self):
        query = normalize_text(self.artist_filter)
        groups = {}
        for release in self._equivalent_editions():
            groups.setdefault(album_key(release), []).append(release)
        rows = []
        for key, editions in groups.items():
            incomplete = [row for row in editions
                          if ((row.get("is_audited") and row.get("missing_count", 0) > 0)
                              or (self.show_unverified and not row.get("is_audited")))]
            if not incomplete or (query and not any(
                query in normalize_text(credit or "") for row in editions
                for credit in [row.get("artist"), row.get("album_artist")]
                + [track.get("artist") for track in row.get("tracks", [])]
            )):
                continue
            preferred = self.selected_editions.get(key)
            selected = self.releases.get(preferred)
            if selected and album_key(selected) != key:
                selected = None
            rows.append(selected or min(incomplete, key=lambda row: (
                row.get("missing_count", 0), release_key(row) != self.selected_key,
                str(release_key(row)),
            )))
        return sorted(rows, key=lambda row: (
            row.get("artist", "").casefold(), row.get("title", "").casefold(),
        ))

    def selected(self):
        rows = self.visible()
        selected = next((r for r in rows if release_key(r) == self.selected_key), None)
        if selected is None:
            selected = rows[0] if rows else None
            self.selected_key = release_key(selected) if selected else None
            self.track_index = 0
        if selected:
            self.track_index = min(self.track_index, max(0, len(selected.get("tracks", [])) - 1))
        return selected

    def update(self, release, old_key=None):
        key = release_key(release)
        if old_key is not None and old_key != key:
            self.releases.pop(old_key, None)
            for history in (self.queued, self.matched, self.downloaded):
                if old_key in history:
                    history.setdefault(key, set()).update(history.pop(old_key))
            if old_key in self.stale:
                # Stale sets are snapshots; one published for the new key is newer.
                self.stale.setdefault(key, self.stale.pop(old_key))
            if self.selected_key == old_key:
                self.selected_key = key
            self._history_releases.pop(old_key, None)
            for album, selected in list(self.selected_editions.items()):
                if selected == old_key:
                    self.selected_editions.pop(album)
                    self.selected_editions[album_key(release)] = key
        self.releases[key] = release
        if key in self._history_releases or any(key in history for history in
                                               (self.queued, self.matched, self.downloaded, self.stale)):
            self._history_releases[key] = release
        if old_key is not None:
            self.preferred_editions[equivalent_release_key(release)] = key

    def move(self, delta, tracks=False):
        selected = self.selected()
        if not selected:
            return
        if tracks:
            self.track_index = max(0, min(len(selected.get("tracks", [])) - 1,
                                          self.track_index + delta))
        else:
            rows = self.visible()
            index = next(i for i, row in enumerate(rows) if release_key(row) == self.selected_key)
            self.selected_key = release_key(rows[max(0, min(len(rows) - 1, index + delta))])
            self.track_index = 0

    def pending(self, release, only_track=None, only_tracks=None):
        # A stale transfer no longer holds its track: it may be searched again.
        queued = self.queued_tracks(release) - self.stale_tracks(release)
        return [t for t in release.get("tracks", []) if t.get("status") == "missing"
                and track_key(t) not in queued
                and (only_track is None or track_key(t) == only_track)
                and (only_tracks is None or track_key(t) in only_tracks)]

    def record_result(self, release, result):
        key = release_key(release)
        self._history_releases[key] = copy.deepcopy(release)
        self.matched[key] = {track_key(t) for t in result.get("matched_tracks", [])}
        self.queued.setdefault(key, set()).update(
            track_key(t) for t in result.get("queued_tracks", [])
        )

    def _shared_history(self, release, history):
        equivalent = equivalent_release_key(release)
        keys = {release_key(release)}
        for key in history:
            row = self.releases.get(key, self._history_releases.get(key))
            if row and equivalent_release_key(row) == equivalent:
                keys.add(key)
        return set().union(*(history.get(key, set()) for key in keys))

    def queued_tracks(self, release):
        return self._shared_history(release, self.queued)

    def stale_tracks(self, release):
        return self._shared_history(release, self.stale)

    def track_status(self, release, track):
        if track.get("status") == "found":
            return "found"
        identity = track_key(track)
        if identity in self._shared_history(release, self.downloaded):
            return "downloaded"
        if identity in self.stale_tracks(release):
            return "stale"
        if identity in self.queued_tracks(release):
            return "queued"
        if identity in self._shared_history(release, self.matched):
            return "matched"
        return "missing" if release.get("is_audited") else "unverified"


def download_release(service, release, library_dir, queued=(), only_track=None,
                     preferred_format="flac", search_timeout=30.0, dry_run=False,
                     on_progress=None, excluded_sources=None, only_tracks=None, downloads_dir=None):
    """Recheck both libraries before submitting a selected release's remaining tracks.

    only_tracks limits a request to several tracks (a coalesced recovery); they
    share one release-scope search unless a single one is still missing.
    downloads_dir is slskd's downloads folder when the caller already knows it.
    """
    fresh = service.refresh_release(release, library_dir=library_dir)
    if not fresh.get("is_audited"):
        raise ValueError(fresh.get("audit_error") or "Release is unverified; refresh its MusicBrainz audit first")
    pending = [t for t in fresh.get("tracks", []) if t.get("status") == "missing"
               and track_key(t) not in queued
               and (only_track is None or track_key(t) == only_track)
               and (only_tracks is None or track_key(t) in only_tracks)]
    if not pending:
        return fresh, {"total_missing": 0, "queued_count": 0, "matched_count": 0,
                       "resolved_count": 0, "queued_tracks": [], "matched_tracks": [],
                       "dry_run": dry_run}
    single = only_track is not None or (only_tracks is not None and len(pending) == 1)
    result = service.release_service.download_missing_tracks(
        artist=fresh["artist"], release_title=fresh.get("mb_release_title") or fresh["title"],
        missing_tracks=pending, preferred_format=preferred_format,
        search_timeout=search_timeout, dry_run=dry_run, on_progress=on_progress,
        search_scope="track" if single else "release",
        # The whole official tracklist: its other titles corroborate a peer's
        # folder, and its numbering covers flat and vinyl positions.
        official_tracks=[t for t in fresh.get("tracks", []) if not t.get("is_extra")],
        **({"excluded_sources": excluded_sources} if excluded_sources else {}),
        **({"downloads_dir": downloads_dir} if downloads_dir else {}),
    )
    return fresh, result


def clipped(text, width):
    """Clip to terminal cells, including wide scripts, and remove control characters."""
    output, used = [], 0
    for char in str(text):
        if unicodedata.category(char).startswith("C"):
            char = " "
        size = 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in "WF" else 1)
        if used + size > width:
            break
        output.append(char)
        used += size
    return "".join(output)


@dataclass
class DownloadRequest:
    release: Dict[str, Any]
    only_track: Any = None
    dry_run: bool = False
    recovery: bool = False
    # Several tracks of one release, from coalesced automatic recoveries.
    only_tracks: Any = None

    def scope(self):
        """Return the requested track keys, or None for every missing track."""
        if self.only_tracks is not None:
            return frozenset(self.only_tracks)
        if self.only_track is not None:
            return frozenset([self.only_track])
        return None

    def covers(self, track=None):
        """Whether this request includes a track (None: every missing track)."""
        scope = self.scope()
        return scope is None or (track is not None and track in scope)


class ReleaseBrowser:
    def __init__(self, args, service_factory=LibraryBrowserService):
        self.args = args
        self.service_factory = service_factory
        self.model = BrowserModel(args.artist or "")
        self.events = queue.Queue()
        self.jobs = queue.Queue()
        # The UI dispatches one request at a time, after applying prior results.
        self.pending_downloads = deque()
        self.active_download = None
        self.stopping = threading.Event()
        self.busy = False
        self.scanning = False
        self.operation = ""
        self.quit_requested = False
        self.focus_tracks = False
        self.filter_edit = None
        self.overlay = None
        self.action_index = 0
        self.edition_index = 0
        self.edition_choices = []
        self.message = "Scanning the library…"
        self.error = ""
        self.last_result = ""
        self.refreshed_during_scan = set()
        self.download_error = ""
        # The UI clears this notice once the delayed recovery has run.
        self._retry_notice = ""
        # Only the worker touches transfer watches and the service clients.
        self._downloads = {}
        self._submitted_tracks = {}
        self._failed_sources = {}
        self._recovery_attempts = {}
        self._release_aliases = {}
        self._peer_policy = PeerPolicy()
        # Stale transfers replaced by another source: {source: release key}.
        # Each is stopped at the next poll if it is still waiting.
        self._superseded = {}
        # Recoveries interrupted by an outage: [(due, release, tracks)].
        self._delayed_recoveries = []
        self._transient_failures = {}
        # The last library check error of each watched release: {release key: text}.
        self._refresh_errors = {}
        # slskd's retry budget and downloads folder, read once when available.
        self._retry_attempts = None
        self._retry_max_delay = None
        self._retry_wait = _retry_wait(None)
        self._downloads_dir = None
        self._slskd_settings_loaded = False
        self._slskd_settings_retry_at = 0.0

    @staticmethod
    def _transfer_key(username, filename):
        return source_key(username, filename)

    def _download_key(self, key):
        # A queued job can still contain the pre-audit release identity.
        seen = set()
        while key in self._release_aliases and key not in seen:
            seen.add(key)
            key = self._release_aliases[key]
        return key

    @staticmethod
    def _new_watch(release):
        return {
            "release": copy.deepcopy(release), "files": {}, "completed": set(), "refresh_after": 0,
            # Monotonic time each file finished, to judge a missing import.
            "completed_at": {},
            # Sources whose transfer still waits for a slot after many hours.
            "stale": set(), "published_stale": frozenset(),
            "idle_refresh_after": time.monotonic() + DOWNLOAD_QUEUED_REFRESH_INTERVAL,
            # Monotonic time each unfinished file was first missing from slskd's list.
            "absent": {},
        }

    @staticmethod
    def _stale_tracks(watch):
        """Tracks held only by stale transfers; these may be searched again."""
        files, stale = watch["files"], watch.get("stale", set())
        healthy = {track for source, track in files.items() if source not in stale}
        return {files[source] for source in stale if source in files} - healthy

    def _publish_stale(self, key, watch, message=None):
        tracks = frozenset(self._stale_tracks(watch))
        if tracks != watch.get("published_stale", frozenset()) or message:
            watch["published_stale"] = tracks
            self.events.put(("stale", (key, set(tracks), message)))

    def _update_download_watch(self, release, old_key=None):
        key = release_key(release)
        if old_key is not None and old_key != key:
            self._release_aliases[old_key] = key
            if old_key in self._failed_sources:
                self._failed_sources.setdefault(key, set()).update(self._failed_sources.pop(old_key))
            for track, attempts in self._recovery_attempts.pop(old_key, {}).items():
                counts = self._recovery_attempts.setdefault(key, {})
                counts[track] = counts.get(track, 0) + attempts
            for track, failures in self._transient_failures.pop(old_key, {}).items():
                counts = self._transient_failures.setdefault(key, {})
                counts[track] = counts.get(track, 0) + failures
        if old_key is not None and old_key != key and old_key in self._submitted_tracks:
            self._submitted_tracks.setdefault(key, set()).update(self._submitted_tracks.pop(old_key))
        if old_key is not None and old_key != key and old_key in self._downloads:
            previous = self._downloads.pop(old_key)
            current = self._downloads.setdefault(key, previous)
            if current is not previous:
                current["files"].update(previous["files"])
                current["completed"].update(previous["completed"])
                current.setdefault("completed_at", {}).update(previous.get("completed_at", {}))
                current.setdefault("stale", set()).update(previous.get("stale", set()))
                current.setdefault("absent", {}).update(previous.get("absent", {}))
        watch = self._downloads.get(key)
        if watch is None:
            return
        watch["release"] = copy.deepcopy(release)
        found = {track_key(t) for t in release.get("tracks", []) if t.get("status") == "found"}
        for source, track in watch["files"].items():
            if track in found and source in watch.get("stale", set()) and source not in watch["completed"]:
                # The track came from elsewhere: stop the transfer that never
                # started, so a peer serving it late cannot add a duplicate.
                self._superseded[source] = key
        watch["files"] = {file: track for file, track in watch["files"].items() if track not in found}
        watch["completed"].intersection_update(watch["files"])
        watch.setdefault("stale", set()).intersection_update(watch["files"])
        completed_at = watch.setdefault("completed_at", {})
        for source in set(completed_at) - watch["completed"]:
            del completed_at[source]
        absent = watch.setdefault("absent", {})
        for source in set(absent) - set(watch["files"]):
            del absent[source]
        self._publish_stale(key, watch)
        if not watch["files"]:
            del self._downloads[key]

    def _remember_downloads(self, release, result):
        if result.get("dry_run"):
            return
        key = release_key(release)
        self._submitted_tracks.setdefault(key, set()).update(
            track_key(track) for track in result.get("queued_tracks", [])
        )
        files = {
            self._transfer_key(file.get("user"), file.get("filename")): track_key(track)
            for file, track in zip(result.get("queued_files", []), result.get("queued_tracks", []))
            if file.get("user") and file.get("filename")
        }
        if files:
            watch = self._downloads.setdefault(key, self._new_watch(release))
            for source in files:
                # A source queued again is wanted, not a leftover to stop.
                self._superseded.pop(source, None)
            replaced = set(files.values())
            for old, track in list(watch["files"].items()):
                if track in replaced and old not in files and old not in watch["completed"]:
                    # Another source now holds this track. A replaced stale
                    # transfer is stopped if it still waits, so a peer serving
                    # it late cannot create a duplicate.
                    del watch["files"][old]
                    if old in watch["stale"]:
                        watch["stale"].discard(old)
                        self._superseded[old] = key
            watch["files"].update(files)
            self._update_download_watch(release)
            self._supersede_equivalent_stale(release, files)

    def _supersede_equivalent_stale(self, release, files):
        """Hand stale tracks of an equivalent edition's watch over to their replacement."""
        key, equivalent, replaced = release_key(release), equivalent_release_key(release), set(files.values())
        for other_key, other in list(self._downloads.items()):
            if other_key == key or equivalent_release_key(other["release"]) != equivalent:
                continue
            handed_over = {}
            for old, track in list(other["files"].items()):
                if track in replaced and old in other["stale"] and old not in files and old not in other["completed"]:
                    handed_over[old] = track
            if not handed_over:
                continue
            for old in handed_over:
                del other["files"][old]
                other["stale"].discard(old)
                self._superseded[old] = other_key
            tracks = set(handed_over.values()) - set(other["files"].values())
            self._submitted_tracks.get(other_key, set()).difference_update(tracks)
            # The replacement's release now holds these tracks in the browser.
            self.events.put(("replaced", (other_key, tracks)))
            self._update_download_watch(other["release"])

    def _monitoring(self):
        return bool(self._downloads or self._superseded)

    def _load_slskd_settings(self, client):
        """Read slskd's retry budget and downloads folder once (best effort)."""
        if self._slskd_settings_loaded or time.monotonic() < self._slskd_settings_retry_at:
            return
        try:
            options = client.get_options()
        except Exception:
            options = None
            self._slskd_settings_retry_at = time.monotonic() + DOWNLOAD_OPTIONS_RETRY_INTERVAL
        else:
            self._slskd_settings_loaded = True
        self._retry_attempts, self._retry_max_delay = retry_budget(options)
        self._retry_wait = _retry_wait(options)
        # Honors DISCOGSEEK_SLSKD_DOWNLOADS_DIR; ignores folders not on this machine.
        self._downloads_dir = resolve_downloads_dir(SimpleNamespace(get_options=lambda: options))

    def _stop_superseded(self, client, transfers):
        """Stop replaced stale transfers that are still waiting for a slot."""
        for source in sorted(self._superseded):
            del self._superseded[source]
            file = transfers.get(source)
            if not file or terminal_failure(file) or not file.get("id"):
                continue
            states = transfer_states(file)
            if not states & WAITING_STATES or states & {"InProgress", "Completed", "Succeeded"}:
                continue
            user, transfer_id = file.get("username") or source[0], file["id"]
            try:
                self._peer_policy.record_internal_cancel(user, transfer_id)
                client.cancel_download(user, transfer_id)
            except Exception as exc:
                message = f"Could not stop the replaced transfer from {user}: {exc}"
                try:
                    self._peer_policy.forget_internal_cancel(user, transfer_id)
                except Exception as cleanup_exc:
                    message += f" Could not clear cancellation marker: {cleanup_exc}."
                self.events.put(("recovery_error", message))

    def _system_cancellations(self, downloads):
        try:
            return set(self._peer_policy.system_cancellations(downloads))
        except Exception:
            return set()

    def _recover_failed_transfer(self, service, key, watch, source, file, system_cancelled=frozenset()):
        """Stop this session's failed transfer before considering another source."""
        state = terminal_failure(file)
        user = file.get("username") or source[0]
        details = f"{user}: {state}"
        if file.get("attempts") is not None:
            details += f" (slskd attempts: {file['attempts']})"
        if file.get("exception"):
            details += f" — {clipped(str(file['exception']), 240)}"
        if state == "Cancelled" and (peer_key(user), source[1], str(file.get("id") or "")) in system_cancelled:
            # slskd stopped many transfers at once: no judgment of this source.
            return details + "; stopped together with other transfers. Press d or t to retry.", False
        self._failed_sources.setdefault(key, set()).add(source)
        if state not in DOWNLOAD_RECOVERY_STATES:
            return details + "; press d or t to retry.", False
        if self.stopping.is_set() or self.args.dry_run:
            return details + "; automatic recovery stopped.", False
        if state in DOWNLOAD_CANCEL_STATES:
            message = self._stop_failed_transfer(service, user, file, details)
            if message:
                return message, False
        count = self._recovery_attempts.get(key, {}).get(watch["files"][source], 0)
        if count >= DOWNLOAD_RECOVERY_LIMIT:
            return details + f"; automatic recovery stopped after {count} searches. Press d or t to retry.", False
        return details + f"; trying another source ({count + 1}/{DOWNLOAD_RECOVERY_LIMIT}).", True

    def _stop_failed_transfer(self, service, user, file, details):
        """Stop slskd's own retries; return an error message if that is unsafe."""
        transfer_id = file.get("id")
        if not transfer_id:
            message = details + "; transfer ID unavailable, so automatic recovery cannot stop slskd retries. Press d or t to retry."
            self.events.put(("recovery_error", message))
            return message
        try:
            # Record before cancellation: a later poll must not mistake our
            # retry cleanup for a user choosing to block this peer.
            self._peer_policy.record_internal_cancel(user, transfer_id)
            service.release_service.slskd_client.cancel_download(user, transfer_id)
        except Exception as exc:
            message = details + f"; could not stop slskd retries: {exc}. Press d or t to retry."
            try:
                # An unsuccessful attempt must not exempt a later user cancel.
                self._peer_policy.forget_internal_cancel(user, transfer_id)
            except Exception as cleanup_exc:
                message += f" Could not clear cancellation marker: {cleanup_exc}."
            self.events.put(("recovery_error", message))
            return message
        return None

    def _final_failure(self, watch, source, file, now, wall_clock):
        """Whether a failed transfer has ended, rather than awaiting slskd's own retry."""
        waiting = watch.setdefault("retry_wait", {})
        if not is_final_failure(file, self._retry_attempts, wall_clock):
            waiting.pop(source, None)
            return False
        attempts = _attempt_count(file)
        if (terminal_failure(file) not in ("TimedOut", "Errored") or file.get("nextAttemptAt")
                or self._retry_attempts is None or attempts >= self._retry_attempts):
            waiting.pop(source, None)
            return True
        # slskd 0.26 keeps one record across retries and does not report the
        # next attempt: wait out its longest delay before switching sources.
        signature = (str(file.get("id") or ""), attempts, file.get("state"))
        seen = waiting.get(source)
        if seen is None or seen[0] != signature:
            waiting[source] = (signature, now)
            return False
        return now - seen[1] >= self._retry_wait

    def _flag_stale(self, key, watch, transfers, wall_clock, serving=frozenset()):
        """Mark transfers still waiting for a slot after hours; search again once.

        The stale transfer stays queued and watched: the downloader cancels it
        only after queueing a replacement, so nothing is lost when no other
        source exists. A peer slowly working through a long queue is not stale.
        """
        stale = {source for source in watch["files"] if source not in watch["completed"]
                 and peer_key(source[0]) not in serving
                 and is_stale_queued(transfers.get(source, {}), wall_clock)}
        newly_stale = stale - watch["stale"]
        watch["stale"] = stale
        recover, details = set(), []
        for source in sorted(newly_stale):
            track, file = watch["files"][source], transfers[source]
            detail = (f"{file.get('username') or source[0]}: "
                      f"{stale_description(file, wall_clock)}; still queued in slskd")
            count = self._recovery_attempts.get(key, {}).get(track, 0)
            if self.stopping.is_set() or self.args.dry_run:
                detail += "."
            elif count >= DOWNLOAD_RECOVERY_LIMIT:
                detail += (f", automatic recovery stopped after {count} searches. "
                           "Press d or t to search again.")
            else:
                detail += f", searching for another source ({count + 1}/{DOWNLOAD_RECOVERY_LIMIT})."
                recover.add(track)
            details.append(detail)
        return recover, details

    def _demote_unimported(self, key, fresh):
        """Return finished files that never reached the library to missing.

        A file still in slskd's downloads folder may yet be imported and stays
        DOWNLOADED. Without access to that folder, wait for the import window.
        An unfinished transfer that left slskd's list (removed there) is
        released the same way once it has been gone for the grace period.
        """
        watch = self._downloads.get(key)
        if watch is None or not (watch["completed"] or watch.get("absent")):
            return
        missing = {track_key(t) for t in fresh.get("tracks", []) if t.get("status") == "missing"}
        now = time.monotonic()
        grace = DOWNLOAD_IMPORT_GRACE if self._downloads_dir else transfer_history.IMPORT_WINDOW_SECONDS
        completed_at = watch.setdefault("completed_at", {})
        absent = watch.setdefault("absent", {})
        demoted, removed = {}, {}
        for source in sorted(watch["completed"]):
            track = watch["files"].get(source)
            if track not in missing or now - completed_at.setdefault(source, now) < grace:
                continue
            if self._downloads_dir and local_download_exists(self._downloads_dir, source[1]):
                continue
            demoted[source] = track
        for source in sorted(set(absent) - watch["completed"]):
            # Without the downloads folder, a finished file that slskd no longer
            # lists may still await its import.
            track = watch["files"].get(source)
            if track in missing and now - absent[source] >= grace:
                removed[source] = track
        if not demoted and not removed:
            return
        for source in {**demoted, **removed}:
            watch["files"].pop(source, None)
            watch["completed"].discard(source)
            completed_at.pop(source, None)
            absent.pop(source, None)
        # Retrying the same file would most likely be skipped (or removed) again.
        self._failed_sources.setdefault(key, set()).update(demoted, removed)
        self._submitted_tracks.get(key, set()).difference_update(set(demoted.values()) | set(removed.values()))
        if demoted:
            tracks = set(demoted.values())
            where = ("is no longer in slskd's downloads folder" if self._downloads_dir
                     else f"finished over {_duration(grace)} ago")
            users = ", ".join(sorted({source[0] for source in demoted}))
            self.events.put(("unimported", (key, tracks, (
                f"{len(tracks)} downloaded track(s) from {users} are not in the library and the file {where}; "
                "press d or t to retry."
            ))))
        if removed:
            tracks = set(removed.values())
            users = ", ".join(sorted({source[0] for source in removed}))
            self.events.put(("failed", (key, tracks, (
                f"{len(tracks)} queued track(s) from {users} are no longer listed in slskd and not in the "
                "library; press d or t to retry."
            ))))
        self._publish_stale(key, watch)
        if not watch["files"]:
            del self._downloads[key]

    @staticmethod
    def _import_check_interval(watch, now):
        """Check new downloads often; files waiting long for an import less often.

        A transfer missing from slskd's list counts as waiting since it vanished.
        """
        completed, completed_at = watch["completed"], watch.get("completed_at", {})
        waiting = [completed_at.get(source, now) for source in completed]
        waiting += [since for source, since in watch.get("absent", {}).items() if source not in completed]
        if waiting and all(now - since >= DOWNLOAD_IMPORT_GRACE for since in waiting):
            return DOWNLOAD_QUEUED_REFRESH_INTERVAL
        return DOWNLOAD_IMPORT_RETRY_INTERVAL

    def _check_downloads(self, service):
        if not self._monitoring() or self.stopping.is_set():
            return
        client = service.release_service.slskd_client
        downloads = []
        try:
            transfers = {}
            if self._superseded or any(set(w["files"]) - w["completed"] for w in self._downloads.values()):
                downloads = client.get_downloads()
                # Observe the complete history before retiring tracked failures,
                # including rejected/cancelled downloads from earlier sessions.
                self._peer_policy.observe(downloads)
                for user in downloads:
                    for directory in user.get("directories", []):
                        for file in directory.get("files", []):
                            key = self._transfer_key(user.get("username"), file.get("filename"))
                            previous = transfers.get(key)
                            if previous and not _is_later_attempt(file, previous):
                                continue
                            transfers[key] = dict(file, username=user.get("username"))
        except Exception as exc:
            self.events.put(("download_error", f"Download updates: {exc}; retrying automatically."))
            return
        self._load_slskd_settings(client)
        self._stop_superseded(client, transfers)
        now, wall_clock = time.monotonic(), time.time()
        system_cancelled = None
        # The downloader judges stale transfers by the same rule.
        serving = transfer_history.serving_peers(downloads, wall_clock)
        refreshes = []
        for key, watch in list(self._downloads.items()):
            if self.stopping.is_set():
                break
            # An unfinished transfer can leave slskd's list (removed there). Its
            # file in slskd's downloads folder shows that it finished.
            absent = watch.setdefault("absent", {})
            for source in watch["files"]:
                if source in transfers or source in watch["completed"]:
                    absent.pop(source, None)
                else:
                    absent.setdefault(source, now)
            completed = {file for file in watch["files"]
                         if "Succeeded" in transfer_states(transfers.get(file, {}))
                         or (file in absent and self._downloads_dir
                             and local_download_exists(self._downloads_dir, file[1]))}
            newly_completed = completed - watch["completed"]
            watch["completed"].update(completed)
            for source in newly_completed:
                absent.pop(source, None)
                # Old history counts from when slskd finished the file.
                ended = parse_timestamp(transfers.get(source, {}).get("endedAt"))
                age = max(0.0, wall_clock - ended) if ended is not None else 0.0
                watch.setdefault("completed_at", {})[source] = now - age
            if newly_completed:
                self.events.put(("downloaded", (key, {watch["files"][file] for file in newly_completed})))
            # A timed-out or errored transfer that slskd will retry is still alive.
            failed = {file for file in watch["files"] if file not in watch["completed"]
                      and self._final_failure(watch, file, transfers.get(file, {}), now, wall_clock)}
            retry_wait = watch.get("retry_wait", {})
            for source in [source for source in retry_wait if source in failed or source not in watch["files"]
                           or source in watch["completed"]]:
                del retry_wait[source]
            recover = set()
            if failed:
                if system_cancelled is None and any(
                        terminal_failure(transfers[file]) == "Cancelled" for file in failed):
                    system_cancelled = self._system_cancellations(downloads)
                details = []
                for source in sorted(failed):
                    message, should_recover = self._recover_failed_transfer(
                        service, key, watch, source, transfers[source], system_cancelled or frozenset(),
                    )
                    details.append(message)
                    if should_recover:
                        recover.add(watch["files"][source])
                tracks = {watch["files"].pop(file) for file in failed}
                watch["stale"].difference_update(failed)
                self._submitted_tracks.get(key, set()).difference_update(tracks)
                self.events.put(("failed", (key, tracks, "; ".join(dict.fromkeys(details)))))
            stale_recover, stale_details = self._flag_stale(key, watch, transfers, wall_clock, serving)
            recover |= stale_recover
            stale_message = None
            if stale_details:
                stale_message = stale_details[0] + (
                    f" ({len(stale_details) - 1} more stale transfer(s))" if len(stale_details) > 1 else ""
                )
            self._publish_stale(key, watch, stale_message)
            if recover:
                # One request per release covers all of this poll's tracks.
                self.events.put(("recover", (copy.deepcopy(watch["release"]), recover)))
            if not watch["files"]:
                del self._downloads[key]
                continue
            # A cleared transfer may already be imported. Only a library audit
            # can mark it found; terminal failures are never treated as success.
            if not watch["completed"] and all(file in transfers for file in watch["files"]):
                # Everything still waits: check the library only occasionally.
                if now >= watch.get("idle_refresh_after", 0):
                    refreshes.append((key, watch))
                continue
            if newly_completed or now >= watch["refresh_after"]:
                refreshes.append((key, watch))
        # Show every known completion before potentially slow library audits.
        for key, watch in refreshes:
            if self.stopping.is_set():
                break
            try:
                fresh = service.refresh_release(watch["release"], library_dir=self.args.music_dir)
                if not fresh.get("is_audited"):
                    raise ValueError(fresh.get("audit_error") or "Release audit unavailable")
                self._update_download_watch(fresh, key)
                self.events.put(("release", (key, fresh)))
                self._demote_unimported(release_key(fresh), fresh)
                self._refresh_errors.pop(key, None)
            except Exception as exc:
                self._refresh_errors[key] = str(exc)
            finally:
                checked = time.monotonic()
                watch["refresh_after"] = checked + self._import_check_interval(watch, checked)
                watch["idle_refresh_after"] = checked + DOWNLOAD_QUEUED_REFRESH_INTERVAL
        # A failed library check stays shown until that release's next check,
        # which may be minutes away, rather than only until the next poll.
        for key in set(self._refresh_errors) - set(self._downloads):
            del self._refresh_errors[key]
        errors = list(dict.fromkeys(self._refresh_errors.values()))
        message = f"Download updates: {'; '.join(errors)}; retrying automatically." if errors else ""
        self.events.put(("download_error", message))

    def _progress(self, done, total, message, unit=None):
        if self.stopping.is_set() and self.operation == "scan":
            raise _ScanStopped()
        if total > 0:
            message = (f"{done}/{total} {unit} · {message}" if unit
                       else f"{message} ({done}/{total})")
        self.events.put(("progress", message))

    def _download_progress(self, done, total, message):
        self._progress(done, total, message, unit="tracks matched")

    def _worker(self):
        service = None
        scan = None
        next_download_check = time.monotonic() + DOWNLOAD_POLL_INTERVAL
        while True:
            self._release_due_recoveries()
            # A backlog of user requests must not starve completion/failure checks.
            if (self._monitoring() and not self.stopping.is_set()
                    and time.monotonic() >= next_download_check):
                job = ("check_downloads", None)
            else:
                job = self._next_job(scan, next_download_check)
            if job is None:
                return
            kind, payload = job
            if kind == "wake":
                continue
            try:
                if service is None:
                    service = self.service_factory()
                if kind == "scan":
                    scan = iter(service.iter_releases(
                        library_dir=self.args.music_dir, force_refresh=self.args.force_refresh,
                        on_progress=self._progress,
                    ))
                elif kind == "scan_step":
                    if self.stopping.is_set():
                        raise _ScanStopped()
                    self.events.put(("release", (None, next(scan))))
                elif kind == "refresh":
                    release = service.refresh_release(payload, library_dir=self.args.music_dir,
                                                      force_refresh=True)
                    self._update_download_watch(release, release_key(payload))
                    self.events.put(("release", (release_key(payload), release)))
                elif kind == "check_downloads":
                    self._check_downloads(service)
                else:
                    self._run_download(service, kind, payload)
            except (StopIteration, _ScanStopped):
                scan = None
                self.events.put(("done", "scan"))
            except Exception as exc:
                self.events.put(("error", str(exc)))
                if kind in ("scan", "scan_step"):
                    scan = None
                    self.events.put(("done", "scan"))
            finally:
                if kind == "check_downloads":
                    next_download_check = time.monotonic() + DOWNLOAD_POLL_INTERVAL
                elif kind not in ("scan", "scan_step"):
                    self.events.put(("done", kind))

    def _run_download(self, service, kind, payload):
        release, queued, only_track, dry_run = payload[:4]
        only_tracks = payload[4] if len(payload) > 4 else None
        # A failure poll may have invalidated the UI's queued snapshot.
        key = self._download_key(release_key(release))
        queued = set(self._submitted_tracks.get(key, queued))
        stale_sources = set()
        for sibling_key, watch in self._downloads.items():
            if equivalent_release_key(watch["release"]) == equivalent_release_key(release):
                queued.update(self._submitted_tracks.get(sibling_key, set()))
        for watch in self._downloads.values():
            if equivalent_release_key(watch["release"]) == equivalent_release_key(release):
                # A stale transfer no longer holds its track. Its source is
                # excluded, and the downloader cancels it only once another
                # source is queued for that track.
                replaceable = self._stale_tracks(watch)
                queued.difference_update(replaceable)
                stale_sources.update(source for source in watch["stale"]
                                     if watch["files"].get(source) in replaceable)
        attempts = requested = None
        if kind == "recover":
            if self.stopping.is_set():
                return
            requested = (set(only_tracks) if only_tracks is not None
                         else {only_track} if only_track is not None else None)
        if requested is not None:
            counts = self._recovery_attempts.setdefault(key, {})
            requested = {track for track in requested if counts.get(track, 0) < DOWNLOAD_RECOVERY_LIMIT}
            if not requested:
                return
            for track in requested:
                counts[track] = counts.get(track, 0) + 1
            attempts = max(counts[track] for track in requested)
            only_track, only_tracks = ((next(iter(requested)), None) if len(requested) == 1
                                       else (None, frozenset(requested)))
        excluded = set(self._failed_sources.get(key, set())) | stale_sources
        try:
            # The downloader judges slskd's history like the polls do: with the
            # same retry budget and downloads folder, read once per session.
            self._load_slskd_settings(getattr(service.release_service, "slskd_client", None))
            with slskd_retry_budget(self._retry_attempts, self._retry_max_delay):
                fresh, result = download_release(
                    service, release, self.args.music_dir, queued, only_track,
                    self.args.format, self.args.timeout, dry_run, self._download_progress,
                    excluded_sources=excluded or None, only_tracks=only_tracks,
                    downloads_dir=self._downloads_dir,
                )
        except Exception as exc:
            if requested is not None and _is_transient_error(exc) and not self.stopping.is_set():
                self._defer_recovery(key, release, requested, exc)
                return
            raise
        if requested is not None:
            result = dict(result, recovery_attempt=attempts)
            for track in requested:
                self._transient_failures.get(key, {}).pop(track, None)
        self._update_download_watch(fresh, release_key(release))
        self._remember_downloads(fresh, result)
        self.events.put(("result", (release_key(release), fresh, result)))

    def _defer_recovery(self, key, release, tracks, error):
        """Refund a recovery attempt lost to a service outage and retry it later."""
        counts = self._recovery_attempts.setdefault(key, {})
        for track in tracks:
            counts[track] = max(0, counts.get(track, 0) - 1)
        failures = self._transient_failures.setdefault(key, {})
        retry = {track for track in tracks if failures.get(track, 0) < DOWNLOAD_RECOVERY_TRANSIENT_LIMIT}
        title = release.get("title", "Release")
        if not retry:
            self.events.put(("recovery_error", (
                f"{title}: recovery search failed: {error}. Automatic recovery stopped after "
                f"{DOWNLOAD_RECOVERY_TRANSIENT_LIMIT} retries; press d or t to retry."
            )))
            return
        for track in retry:
            failures[track] = failures.get(track, 0) + 1
        count = max(failures[track] for track in retry)
        delay = min(DOWNLOAD_RECOVERY_BACKOFF * 2 ** (count - 1), DOWNLOAD_RECOVERY_BACKOFF_MAX)
        self._delayed_recoveries.append((time.monotonic() + delay, copy.deepcopy(release), frozenset(retry)))
        self.events.put(("recovery_retry", (
            f"{title}: recovery search failed: {error}; retrying in {_duration(delay)} "
            f"({count}/{DOWNLOAD_RECOVERY_TRANSIENT_LIMIT})."
        )))

    def _next_recovery_due(self):
        if not self._delayed_recoveries or self.stopping.is_set():
            return None
        return min(due for due, _, _ in self._delayed_recoveries)

    def _release_due_recoveries(self):
        due = self._next_recovery_due()
        now = time.monotonic()
        if due is None or due > now:
            return
        ready = [entry for entry in self._delayed_recoveries if entry[0] <= now]
        self._delayed_recoveries = [entry for entry in self._delayed_recoveries if entry[0] > now]
        for _, release, tracks in ready:
            self.events.put(("recover", (release, set(tracks))))

    def _next_job(self, scan, next_download_check):
        while True:
            try:
                return self.jobs.get_nowait()
            except queue.Empty:
                now = time.monotonic()
                checking = self._monitoring() and not self.stopping.is_set()
                deadlines = [next_download_check] if checking else []
                recovery_due = self._next_recovery_due()
                if recovery_due is not None:
                    deadlines.append(recovery_due)
                timeout = max(0, min(deadlines) - now) if deadlines else None
                if timeout == 0:
                    if checking and now >= next_download_check:
                        return "check_downloads", None
                    return "wake", None
                elif scan is not None:
                    return "scan_step", None
                else:
                    try:
                        return self.jobs.get(timeout=timeout)
                    except queue.Empty:
                        continue

    def _start(self, kind, payload=None, clear_error=True):
        if self.busy and (self.operation != "scan" or kind == "scan"):
            self.message = f"{self.operation.capitalize()} in progress; you can still browse and filter."
            return False
        self.busy, self.operation = True, kind
        if kind == "scan":
            self.scanning = True
            self.refreshed_during_scan.clear()
            self.model.preferred_editions.clear()
        if clear_error:
            self.error = ""
        self.message = {"scan": "Scanning the library and loading saved audits…", "refresh": "Refreshing selected release…",
                        "download": "Checking the library, then searching for missing tracks…",
                        "recover": "Checking the library, then searching for another source…"}[kind]
        self.jobs.put((kind, payload))
        return True

    def _update_release(self, release, old_key=None):
        self.model.update(release, old_key)
        if self.overlay == "editions":
            # Keep the menu order stable as scan and transfer events arrive.
            self.edition_choices = [release if release_key(row) in (old_key, release_key(release))
                                    else row for row in self.edition_choices]
        requests = list(self.pending_downloads)
        if self.active_download is not None:
            requests.append(self.active_download)
        for request in requests:
            if release_key(request.release) in (old_key, release_key(release)):
                request.release = copy.deepcopy(release)

    def _start_next_download(self):
        if self.quit_requested or (self.busy and self.operation != "scan"):
            return
        while self.pending_downloads:
            request = self.pending_downloads.popleft()
            key = release_key(request.release)
            release = self.model.releases.get(key, request.release)
            if not self.model.pending(release, request.only_track, request.only_tracks):
                continue
            request.release = copy.deepcopy(release)
            self.active_download = request
            payload = (copy.deepcopy(release),
                       self.model.queued_tracks(release) - self.model.stale_tracks(release),
                       request.only_track, request.dry_run)
            if request.only_tracks is not None:
                payload += (frozenset(request.only_tracks),)
            self._start("recover" if request.recovery else "download", payload, clear_error=False)
            action = "Previewing" if request.dry_run else "Downloading"
            self.message = f"{action} {release['title']}. Checking the library first…"
            return

    def _drain_events(self):
        while True:
            try:
                kind, data = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                self.message = data
            elif kind == "release":
                old_key, release = data
                key = release_key(release)
                if old_key is not None:
                    if self.scanning:
                        self.refreshed_during_scan.update(identity for identity in
                            (old_key, key, equivalent_release_key(release)) if identity is not None)
                    self._update_release(release, old_key)
                elif (key not in self.refreshed_during_scan
                      and equivalent_release_key(release) not in self.refreshed_during_scan):
                    self._update_release(release)
            elif kind == "error":
                self.error = data
            elif kind == "download_error":
                self.download_error = data
            elif kind == "recovery_error":
                self.error = data
            elif kind == "recovery_retry":
                self.error = self._retry_notice = data
            elif kind == "downloaded":
                key, tracks = data
                self.model.downloaded.setdefault(key, set()).update(tracks)
                self.message = f"{len(tracks)} download(s) finished. Checking the library automatically…"
            elif kind == "stale":
                key, tracks, detail = data
                self.model.stale[key] = set(tracks)
                if detail:
                    release = self.model.releases.get(key, {})
                    self.last_result = f"{release.get('title', 'Release')}: {detail}"
                    self.message = self.last_result
            elif kind == "replaced":
                key, tracks = data
                for history in (self.model.queued, self.model.matched, self.model.stale):
                    history.get(key, set()).difference_update(tracks)
            elif kind in ("failed", "unimported"):
                key, tracks, detail = data
                for history in (self.model.queued, self.model.matched, self.model.downloaded, self.model.stale):
                    history.get(key, set()).difference_update(tracks)
                release = self.model.releases.get(key, {})
                self.last_result = f"{release.get('title', 'Release')}: {detail}"
                self.message = self.last_result
            elif kind == "recover":
                release, tracks = data
                if not self.quit_requested:
                    self._queue_recovery(release, tracks)
            elif kind == "result":
                old_key, release, result = data
                if self.scanning:
                    self.refreshed_during_scan.update(identity for identity in
                        (old_key, release_key(release), equivalent_release_key(release)) if identity is not None)
                self._update_release(release, old_key)
                self.model.record_result(release, result)
                unresolved = result.get("total_missing", 0) - result.get("resolved_count", 0)
                count = result.get("matched_count", 0) if result.get("dry_run") else result.get("queued_count", 0)
                action = "matched (preview; nothing queued)" if result.get("dry_run") else "queued in slskd"
                self.last_result = f"{release['title']}: {count} {action}; {unresolved} unmatched."
                if result.get("recovery_attempt"):
                    if self._retry_notice and self.error == self._retry_notice:
                        self.error = ""
                    self._retry_notice = ""
                    self.last_result += f" Recovery {result['recovery_attempt']}/{DOWNLOAD_RECOVERY_LIMIT}."
                    if not count and result.get("total_missing", 0):
                        self.last_result += " No alternative queued; press d or t to retry."
                errors = result.get("queue_errors", [])
                if errors:
                    self.error = "; ".join(e.get("error", str(e)) if isinstance(e, dict) else str(e)
                                           for e in errors)
                self.message = self.last_result
            elif kind == "done":
                if data == "scan":
                    self.scanning = False
                    if self.operation == "scan":
                        self.busy = False
                    self.message = f"Scan finished: {len(self.model.releases)} releases checked."
                else:
                    if data in ("download", "recover"):
                        self.active_download = None
                    self.busy = self.scanning
                    if self.scanning:
                        self.operation = "scan"
                    if data == "refresh":
                        self.message = "Release refreshed from the library and MusicBrainz."
        self._start_next_download()

    def _queue_recovery(self, release, tracks):
        """Add failed tracks to one automatic recovery request per release."""
        key = release_key(release)
        requests = list(self.pending_downloads)
        if self.active_download is not None:
            requests.append(self.active_download)
        tracks = {track for track in tracks if not any(
            release_key(request.release) == key and not request.dry_run and request.covers(track)
            for request in requests
        )}
        if not tracks:
            return
        for request in self.pending_downloads:
            if request.recovery and not request.dry_run and release_key(request.release) == key:
                request.only_tracks = request.scope() | tracks
                request.only_track = None
                return
        if len(tracks) == 1:
            request = DownloadRequest(copy.deepcopy(release), next(iter(tracks)), recovery=True)
        else:
            request = DownloadRequest(copy.deepcopy(release), recovery=True, only_tracks=frozenset(tracks))
        self.pending_downloads.append(request)

    def _download(self, single=False, preview=False):
        if self.quit_requested:
            return
        release = self.model.selected()
        if not release:
            self.message = "Select an incomplete release first."
            return
        if not release.get("is_audited"):
            self.message = "Unverified release: press r to retry its MusicBrainz audit."
            return
        only_track = None
        if single:
            tracks = release.get("tracks", [])
            if not tracks:
                return
            selected_track = tracks[self.model.track_index]
            only_track = track_key(selected_track)
            if selected_track.get("status") == "found":
                self.message = "Track already in your library. Tab then ↑↓: select a missing track."
                return
        if not self.model.pending(release, only_track):
            self.message = "No unqueued missing tracks in this selection."
            return
        dry_run = preview or self.args.dry_run
        requests = list(self.pending_downloads)
        if self.active_download is not None:
            requests.append(self.active_download)
        if any(equivalent_release_key(request.release) == equivalent_release_key(release)
               and request.dry_run == dry_run and request.covers(only_track) for request in requests):
            self.message = f"{release['title']}: request already waiting or in progress."
            return
        request = DownloadRequest(copy.deepcopy(release), only_track, dry_run)
        self.pending_downloads.append(request)
        if not self.busy or self.operation == "scan":
            self.error = ""
        self._start_next_download()
        if self.active_download is not request:
            self.message = f"{release['title']}: request added ({len(self.pending_downloads)} waiting)."

    def _open_download_options(self):
        if not self.model.selected():
            self.message = "Select an incomplete release first."
            return
        self.overlay = "downloads"
        self.action_index = 1 if self.focus_tracks else 0

    def _open_editions(self):
        release = self.model.selected()
        if not release:
            self.message = "Select an album first."
            return
        editions = self.model.editions(release)
        if len(editions) < 2:
            self.message = "Only one distinct verified edition for this album."
            return
        self.overlay = "editions"
        self.edition_choices = editions
        self.edition_index = next(index for index, row in enumerate(editions)
                                  if release_key(row) == release_key(release))

    def _handle_overlay_key(self, key, curses):
        if key in ("q", "Q", "\x03"):
            self._quit()
        elif key in ("\x1b", "?"):
            self.overlay = None
        elif self.overlay == "help":
            if key in ("\n", "\r", curses.KEY_ENTER):
                self.overlay = None
        elif self.overlay == "editions":
            editions = self.edition_choices
            if not editions:
                self.overlay = None
                return
            self.edition_index = min(self.edition_index, len(editions) - 1)
            if key in (curses.KEY_UP, "k", curses.KEY_DOWN, "j", "\t", curses.KEY_BTAB):
                delta = -1 if key in (curses.KEY_UP, "k", curses.KEY_BTAB) else 1
                self.edition_index = (self.edition_index + delta) % len(editions)
            elif key in ("\n", "\r", curses.KEY_ENTER):
                edition = editions[self.edition_index]
                self.model.choose_edition(edition)
                self.message = f"Edition selected: {edition_label(edition)}"
                self.overlay = None
        elif key in (curses.KEY_UP, "k", curses.KEY_DOWN, "j", "\t", curses.KEY_BTAB):
            delta = -1 if key in (curses.KEY_UP, "k", curses.KEY_BTAB) else 1
            self.action_index = (self.action_index + delta) % 4
        elif key in ("d", "t", "p", "P", "\n", "\r", curses.KEY_ENTER):
            index = {"d": 0, "t": 1, "p": 2, "P": 3}.get(key, self.action_index)
            self.overlay = None
            self._download(single=index in (1, 3), preview=index in (2, 3))

    def _quit(self):
        self.quit_requested = True
        self.stopping.set()
        waiting = len(self.pending_downloads)
        self.pending_downloads.clear()
        if self.busy:
            self.message = ("Exiting after the current operation finishes…"
                            + (f" Discarded {waiting} waiting request(s)." if waiting else ""))

    def _handle_key(self, key, curses, page_size):
        if self.overlay is not None:
            self._handle_overlay_key(key, curses)
            return
        if self.filter_edit is not None:
            if key in ("\n", "\r", curses.KEY_ENTER):
                self.model.artist_filter = self.filter_edit
                self.filter_edit = None
            elif key == "\x1b":
                self.filter_edit = None
            elif key in ("\b", "\x7f", curses.KEY_BACKSPACE):
                self.filter_edit = self.filter_edit[:-1]
            elif isinstance(key, str) and key.isprintable():
                self.filter_edit += key
            return
        if key in ("q", "Q", "\x03"):
            self._quit()
        elif key in ("\n", "\r", curses.KEY_ENTER):
            self._open_download_options()
        elif key == "?":
            self.overlay = "help"
        elif key == "e":
            self._open_editions()
        elif key == "/":
            self.filter_edit = self.model.artist_filter
        elif key == "\x1b":
            self.model.artist_filter = ""
        elif key in ("\t", curses.KEY_BTAB):
            self.focus_tracks = not self.focus_tracks
        elif key in (curses.KEY_LEFT, "h"):
            self.focus_tracks = False
        elif key in (curses.KEY_RIGHT, "l"):
            self.focus_tracks = True
        elif key in (curses.KEY_UP, "k", curses.KEY_DOWN, "j", curses.KEY_NPAGE, curses.KEY_PPAGE):
            delta = {curses.KEY_UP: -1, "k": -1, curses.KEY_DOWN: 1, "j": 1,
                     curses.KEY_NPAGE: page_size, curses.KEY_PPAGE: -page_size}[key]
            self.model.move(delta, self.focus_tracks)
        elif key in (curses.KEY_HOME, "g", curses.KEY_END, "G"):
            self.model.move(-10**9 if key in (curses.KEY_HOME, "g") else 10**9, self.focus_tracks)
        elif key == "u":
            self.model.show_unverified = not self.model.show_unverified
        elif key == "d":
            self._download()
        elif key == "t":
            self._download(single=True)
        elif key == "p":
            self._download(preview=True)
        elif key == "P":
            self._download(single=True, preview=True)
        elif key == "r" and self.model.selected():
            self._start("refresh", copy.deepcopy(self.model.selected()))
        elif key == "R" and not self.busy:
            self.model.releases.clear()
            self._start("scan")

    def _draw(self, screen, curses):
        height, width = screen.getmaxyx()
        screen.erase()

        def put(y, x, text, cells=None, attr=0):
            if 0 <= y < height and 0 <= x < width - 1:
                try:
                    screen.addstr(y, x, clipped(text, min(cells or width, width - x - 1)), attr)
                except curses.error:
                    pass  # A terminal can resize between measuring and drawing.

        if height < 14 or width < 72:
            put(0, 0, "Enlarge terminal to at least 72 x 14. Press q to quit.")
            put(2, 0, self.error or self.download_error or self.message)
            screen.refresh()
            return
        put(0, 1, "DISCOGSEEK  /  incomplete releases", attr=curses.A_BOLD)
        mode = f"{self.args.format} | {'DRY RUN' if self.args.dry_run else 'slskd downloads'}"
        put(0, max(40, width - len(mode) - 2), mode)
        query = self.filter_edit if self.filter_edit is not None else self.model.artist_filter
        put(1, 1, f"Artist filter: {query or 'all artists'}" + ("_  [Enter apply / Esc cancel]" if self.filter_edit is not None else ""))
        rows, selected = self.model.visible(), self.model.selected()
        unverified = sum(not r.get("is_audited") for r in self.model.releases.values())
        put(2, 1, f"{len(rows)} shown | {unverified} unverified (u: {'hide' if self.model.show_unverified else 'show'}) | "
            + (f"working · {len(self.pending_downloads)} waiting" if self.busy else "ready"))
        split = max(28, min(width * 2 // 5, 52))
        for y in range(3, height - 5):
            put(y, split, "│")
        put(3, 1, "RELEASES · missing / total", split - 2,
            curses.A_BOLD | (curses.A_REVERSE if not self.focus_tracks else 0))
        put(3, split + 2, "TRACKS · disc.track / status / title", attr=curses.A_BOLD |
            (curses.A_REVERSE if self.focus_tracks else 0))
        capacity = max(1, (height - 10) // 2)
        selected_index = next((i for i, r in enumerate(rows) if release_key(r) == self.model.selected_key), 0)
        start = max(0, selected_index - capacity + 1)
        for index, release in enumerate(rows[start:start + capacity], start):
            y = 5 + (index - start) * 2
            active = release_key(release) == self.model.selected_key
            counts = (f"{release.get('missing_count', 0)}/{len(release.get('tracks', []))} missing"
                      if release.get("is_audited") else "unverified")
            request_label = ""
            editions = self.model.editions(release)
            if (self.active_download
                    and album_key(self.active_download.release) == album_key(release)):
                request_label = "[searching] "
            elif any(album_key(request.release) == album_key(release)
                     for request in self.pending_downloads):
                request_label = "[waiting] "
            edition_count = f" [{len(editions)} editions]" if len(editions) > 1 else ""
            put(y, 1, f"{'›' if active else ' '} {request_label}{release['title']}{edition_count}", split - 2,
                curses.A_REVERSE if active else 0)
            put(y + 1, 3, f"{release['artist']} · {counts}", split - 4, curses.A_DIM)
        if not rows:
            put(5, 2, "No incomplete releases.", split - 3)
            put(7, 2, "Scanning…" if self.busy else "Esc: clear · u: unverified", split - 3)
        if selected:
            put(4, split + 2, f"{selected['artist']} — {selected['title']}", attr=curses.A_BOLD)
            tracks = selected.get("tracks", [])
            track_capacity = height - 11
            track_start = max(0, self.model.track_index - track_capacity + 1)
            for index, track in enumerate(tracks[track_start:track_start + track_capacity], track_start):
                status = self.model.track_status(selected, track)
                label = f"{track.get('disc_number') or 1}.{track.get('track_number') or '-'}"
                attr = curses.A_REVERSE if self.focus_tracks and index == self.model.track_index else 0
                if status in ("missing", "unverified", "stale"):
                    attr |= curses.A_BOLD
                credit = track.get("artist")
                title = track.get("title", "")
                if credit and credit != selected.get("artist"):
                    title += f" — {credit}"
                marker = "›" if index == self.model.track_index else " "
                put(5 + index - track_start, split + 2, f"{marker}{label:>5} {status.upper():<10} {title}", attr=attr)
            detail = selected.get("audit_error") or (
                f"Track {self.model.track_index + 1}/{len(tracks)} · {edition_label(selected)}"
            )
            put(height - 6, split + 2, detail, attr=curses.A_DIM)
        put(height - 5, 1, self.error or self.download_error or self.message,
            attr=curses.A_BOLD if self.error or self.download_error else 0)
        put(height - 4, 1, self.last_result or "Downloads update automatically; DOWNLOADED = waiting for library.", attr=curses.A_DIM)
        put(height - 3, 1, "d Download all missing · t Download selected track · Enter options", attr=curses.A_BOLD)
        put(height - 2, 1, "↑↓ move · Tab pane · e editions · / artist · r refresh · ? help · q quit")
        if self.overlay is not None:
            self._draw_overlay(put, height, width, curses)
        screen.refresh()

    def _draw_overlay(self, put, height, width, curses):
        box_width = min(width - 4, 88)
        left, top = (width - box_width) // 2, (height - 12) // 2
        for row in range(top, top + 12):
            put(row, left, " " * box_width, box_width, curses.A_REVERSE)

        def line(row, text, selected=False):
            put(top + row, left + 2, text, box_width - 4,
                curses.A_BOLD if selected else curses.A_REVERSE)

        if self.overlay == "help":
            line(0, "KEYBOARD HELP", True)
            for row, text in enumerate([
                "Enter: download options     d: download all missing tracks",
                "t: download selected track (Tab, then ↑↓ to select)",
                "p: preview all missing     P: preview selected track",
                "↑↓ / j k: move    Tab / ← →: switch panes",
                "PgUp/PgDn, Home/End / g G: move through long lists",
                "/: artist filter    Esc: clear filter    u: unverified",
                "e: choose an album's edition; downloads use that edition",
                "r: refresh edition    R: rescan library, reuse saved audits",
                "d/t requests wait in order; q discards waiting requests",
                "STALE: queued in slskd for hours without starting; d/t search again",
                "Esc / Enter: close help    q: quit",
            ], 1):
                line(row, text)
            return

        if self.overlay == "editions":
            line(0, "ALBUM EDITIONS", True)
            editions = self.edition_choices
            if not editions:
                line(2, "No album selected. Esc to close.")
                return
            release = editions[0]
            line(1, f"{release['artist']} — {release['title']}")
            self.edition_index = min(self.edition_index, max(0, len(editions) - 1))
            line(2, f"MBID: {editions[self.edition_index].get('mb_release_id') or 'unresolved'}")
            start = max(0, self.edition_index - 5)
            for index, edition in enumerate(editions[start:start + 6], start):
                counts = f"{edition.get('missing_count', 0)}/{len(edition.get('tracks', []))} missing"
                marker = "› " if index == self.edition_index else "  "
                line(3 + index - start, f"{marker}{counts} · {edition_label(edition)}",
                     index == self.edition_index)
            line(9, f"Edition {self.edition_index + 1}/{len(editions)} · ↑↓ choose · Enter select · Esc close")
            line(10, "Tracks and downloads follow the selected edition.")
            return
        release = self.model.selected()
        line(0, "DOWNLOAD OPTIONS" + (" · DRY RUN" if self.args.dry_run else ""), True)
        if not release:
            line(2, "No incomplete release selected. Esc to close.")
            return
        line(1, f"Release: {release['title']} · {edition_label(release)}")
        tracks = release.get("tracks", [])
        track = tracks[self.model.track_index] if tracks else None
        status = self.model.track_status(release, track) if track else "unavailable"
        line(2, f"Selected: {track.get('title', '') if track else 'No track'} [{status.upper()}]")
        pending = len(self.model.pending(release))
        verb = "Preview" if self.args.dry_run else "Download"
        actions = [f"[d] {verb} all missing tracks ({pending} remaining)",
                   f"[t] {verb} selected track only",
                   "[p] Preview all missing tracks", "[P] Preview selected track only"]
        for index, text in enumerate(actions):
            line(4 + index, ("› " if index == self.action_index else "  ") + text,
                 index == self.action_index)
        line(9, "↑↓ choose · Enter run · Esc close · Tab in browser selects tracks")
        line(10, "Downloads queue in slskd. Previews only search for matches.")

    def run(self, screen):
        import curses

        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.keypad(True)
        screen.timeout(100)
        worker = threading.Thread(target=self._worker, daemon=True, name="release-browser")
        worker.start()
        self._start("scan")
        try:
            while True:
                self._drain_events()
                if self.quit_requested and not self.busy:
                    break
                try:
                    self._draw(screen, curses)
                    key = screen.get_wch()
                except curses.error:
                    continue
                except KeyboardInterrupt:
                    self._quit()
                    continue
                if not self.quit_requested:
                    height, _ = screen.getmaxyx()
                    page = max(1, height - 11 if self.focus_tracks else (height - 10) // 2)
                    self._handle_key(key, curses, page)
        finally:
            self.stopping.set()
            self.jobs.put(None)
        return 0


def run_browser(args):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("browse needs an interactive terminal; run discogseek browse directly in your terminal")
    try:
        import curses
    except ImportError as exc:
        raise RuntimeError("The release browser requires Python's curses module (Linux/macOS)") from exc
    locale.setlocale(locale.LC_ALL, "")
    app = ReleaseBrowser(args)

    class BrowserLogHandler(logging.Handler):
        def emit(self, record):
            app.events.put(("progress", self.format(record)))

    # Service logs must not write over curses' screen, including --verbose logs.
    root_logger = logging.getLogger()
    handlers = root_logger.handlers[:]
    root_logger.handlers = [BrowserLogHandler()]
    try:
        return curses.wrapper(app.run)
    finally:
        root_logger.handlers = handlers
