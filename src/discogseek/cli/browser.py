"""Two-pane, keyboard-driven release browser using the standard-library terminal UI."""

import copy
import locale
import logging
import queue
import re
import sys
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict

from discogseek.core.text import normalize_text
from discogseek.services.library_browser import LibraryBrowserService


DOWNLOAD_POLL_INTERVAL = 3.0
DOWNLOAD_IMPORT_RETRY_INTERVAL = 15.0
DOWNLOAD_FAILURE_STATES = {"Cancelled", "TimedOut", "Errored", "Rejected", "Aborted"}


class _ScanStopped(Exception):
    """Stop read-only discovery at the next progress callback."""


def release_key(release):
    return release.get("mb_release_id") or release.get("id") or (
        normalize_text(release.get("artist", "")), normalize_text(release.get("title", ""))
    )


def track_key(track):
    def number(value):
        try:
            return str(int(value))
        except (TypeError, ValueError):
            return str(value)

    return (number(track.get("disc_number") or 1), number(track.get("track_number") or ""),
            track.get("mb_track_id") or track.get("mb_recording_id") or track.get("title", ""))


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

    def visible(self):
        query = normalize_text(self.artist_filter)
        return sorted(
            [r for r in self.releases.values()
             if (not query or any(query in normalize_text(credit or "") for credit in
                                  [r.get("artist"), r.get("album_artist")]
                                  + [t.get("artist") for t in r.get("tracks", [])]))
             and ((r.get("is_audited") and r.get("missing_count", 0) > 0)
                  or (self.show_unverified and not r.get("is_audited")))],
            key=lambda r: (r.get("artist", "").casefold(), r.get("title", "").casefold()),
        )

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
            if self.selected_key == old_key:
                self.selected_key = key
        self.releases[key] = release

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

    def pending(self, release, only_track=None):
        queued = self.queued.get(release_key(release), set())
        return [t for t in release.get("tracks", []) if t.get("status") == "missing"
                and track_key(t) not in queued
                and (only_track is None or track_key(t) == only_track)]

    def record_result(self, release, result):
        key = release_key(release)
        self.matched[key] = {track_key(t) for t in result.get("matched_tracks", [])}
        self.queued.setdefault(key, set()).update(
            track_key(t) for t in result.get("queued_tracks", [])
        )

    def track_status(self, release, track):
        if track.get("status") == "found":
            return "found"
        key, identity = release_key(release), track_key(track)
        if identity in self.downloaded.get(key, set()):
            return "downloaded"
        if identity in self.queued.get(key, set()):
            return "queued"
        if identity in self.matched.get(key, set()):
            return "matched"
        return "missing" if release.get("is_audited") else "unverified"


def download_release(service, release, library_dir, queued=(), only_track=None,
                     preferred_format="flac", search_timeout=30.0, dry_run=False,
                     on_progress=None):
    """Recheck both libraries before submitting a selected release's remaining tracks."""
    fresh = service.refresh_release(release, library_dir=library_dir)
    if not fresh.get("is_audited"):
        raise ValueError(fresh.get("audit_error") or "Release is unverified; refresh its MusicBrainz audit first")
    pending = [t for t in fresh.get("tracks", []) if t.get("status") == "missing"
               and track_key(t) not in queued
               and (only_track is None or track_key(t) == only_track)]
    if not pending:
        return fresh, {"total_missing": 0, "queued_count": 0, "matched_count": 0,
                       "resolved_count": 0, "queued_tracks": [], "matched_tracks": [],
                       "dry_run": dry_run}
    result = service.release_service.download_missing_tracks(
        artist=fresh["artist"], release_title=fresh.get("mb_release_title") or fresh["title"],
        missing_tracks=pending, preferred_format=preferred_format,
        search_timeout=search_timeout, dry_run=dry_run, on_progress=on_progress,
        search_scope="track" if only_track is not None else "release",
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
        self.message = "Scanning the library…"
        self.error = ""
        self.last_result = ""
        self.refreshed_during_scan = set()
        self.download_error = ""
        # Only the worker touches transfer watches and the service clients.
        self._downloads = {}
        self._submitted_tracks = {}

    @staticmethod
    def _transfer_key(username, filename):
        username = re.sub(r"\s*\(.*?\)$", "", username or "").strip()
        return username, (filename or "").replace("\\", "/")

    def _update_download_watch(self, release, old_key=None):
        key = release_key(release)
        if old_key is not None and old_key != key and old_key in self._submitted_tracks:
            self._submitted_tracks.setdefault(key, set()).update(self._submitted_tracks.pop(old_key))
        if old_key is not None and old_key != key and old_key in self._downloads:
            previous = self._downloads.pop(old_key)
            current = self._downloads.setdefault(key, previous)
            current["files"].update(previous["files"])
            current["completed"].update(previous["completed"])
        watch = self._downloads.get(key)
        if watch is None:
            return
        watch["release"] = copy.deepcopy(release)
        found = {track_key(t) for t in release.get("tracks", []) if t.get("status") == "found"}
        watch["files"] = {file: track for file, track in watch["files"].items() if track not in found}
        watch["completed"].intersection_update(watch["files"])
        if not watch["files"]:
            del self._downloads[key]

    def _remember_downloads(self, release, result):
        if result.get("dry_run"):
            return
        self._submitted_tracks.setdefault(release_key(release), set()).update(
            track_key(track) for track in result.get("queued_tracks", [])
        )
        files = {
            self._transfer_key(file.get("user"), file.get("filename")): track_key(track)
            for file, track in zip(result.get("queued_files", []), result.get("queued_tracks", []))
            if file.get("user") and file.get("filename")
        }
        if files:
            watch = self._downloads.setdefault(release_key(release), {
                "release": copy.deepcopy(release), "files": {}, "completed": set(), "refresh_after": 0,
            })
            watch["files"].update(files)
            self._update_download_watch(release)

    def _check_downloads(self, service):
        if not self._downloads or self.stopping.is_set():
            return
        try:
            transfers = {}
            if any(set(w["files"]) - w["completed"] for w in self._downloads.values()):
                for user in service.release_service.slskd_client.get_downloads():
                    for directory in user.get("directories", []):
                        for file in directory.get("files", []):
                            key = self._transfer_key(user.get("username"), file.get("filename"))
                            transfers[key] = {part.strip() for part in (file.get("state") or "").split(",")}
        except Exception as exc:
            self.events.put(("download_error", f"Download updates: {exc}; retrying automatically."))
            return
        refreshes = []
        for key, watch in list(self._downloads.items()):
            if self.stopping.is_set():
                break
            completed = {file for file in watch["files"] if "Succeeded" in transfers.get(file, set())}
            newly_completed = completed - watch["completed"]
            watch["completed"].update(completed)
            if newly_completed:
                self.events.put(("downloaded", (key, {watch["files"][file] for file in newly_completed})))
            failed = {file for file in watch["files"] if file not in watch["completed"]
                      and "Completed" in transfers.get(file, set())
                      and DOWNLOAD_FAILURE_STATES.intersection(transfers.get(file, set()))}
            if failed:
                tracks = {watch["files"].pop(file) for file in failed}
                self._submitted_tracks.get(key, set()).difference_update(tracks)
                self.events.put(("failed", (key, tracks)))
                if not watch["files"]:
                    del self._downloads[key]
                    continue
            # A cleared transfer may already be imported. Only a library audit
            # can mark it found; terminal failures are never treated as success.
            if not watch["completed"] and all(file in transfers for file in watch["files"]):
                continue
            if newly_completed or time.monotonic() >= watch["refresh_after"]:
                refreshes.append((key, watch))
        # Show every known completion before potentially slow library audits.
        errors = []
        for key, watch in refreshes:
            if self.stopping.is_set():
                break
            try:
                fresh = service.refresh_release(watch["release"], library_dir=self.args.music_dir)
                if not fresh.get("is_audited"):
                    raise ValueError(fresh.get("audit_error") or "Release audit unavailable")
                self._update_download_watch(fresh, key)
                self.events.put(("release", (key, fresh)))
            except Exception as exc:
                errors.append(str(exc))
            finally:
                watch["refresh_after"] = time.monotonic() + DOWNLOAD_IMPORT_RETRY_INTERVAL
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
            # A backlog of user requests must not starve completion/failure checks.
            if (self._downloads and not self.stopping.is_set()
                    and time.monotonic() >= next_download_check):
                job = ("check_downloads", None)
            else:
                job = self._next_job(scan, next_download_check)
            if job is None:
                return
            kind, payload = job
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
                    release, queued, only_track, dry_run = payload
                    # A failure poll may have invalidated the UI's queued snapshot.
                    queued = self._submitted_tracks.get(release_key(release), queued)
                    fresh, result = download_release(
                        service, release, self.args.music_dir, queued, only_track,
                        self.args.format, self.args.timeout, dry_run, self._download_progress,
                    )
                    self._update_download_watch(fresh, release_key(release))
                    self._remember_downloads(fresh, result)
                    self.events.put(("result", (release_key(release), fresh, result)))
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

    def _next_job(self, scan, next_download_check):
        while True:
            try:
                return self.jobs.get_nowait()
            except queue.Empty:
                timeout = (max(0, next_download_check - time.monotonic())
                           if self._downloads and not self.stopping.is_set() else None)
                if timeout == 0:
                    return "check_downloads", None
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
        if clear_error:
            self.error = ""
        self.message = {"scan": "Scanning the library and loading saved audits…", "refresh": "Refreshing selected release…",
                        "download": "Checking the library, then searching for missing tracks…"}[kind]
        self.jobs.put((kind, payload))
        return True

    def _update_release(self, release, old_key=None):
        self.model.update(release, old_key)
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
            if not self.model.pending(release, request.only_track):
                continue
            request.release = copy.deepcopy(release)
            self.active_download = request
            self._start("download", (copy.deepcopy(release), set(self.model.queued.get(key, set())),
                                     request.only_track, request.dry_run), clear_error=False)
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
                        self.refreshed_during_scan.add(key)
                    self._update_release(release, old_key)
                elif key not in self.refreshed_during_scan:
                    self._update_release(release)
            elif kind == "error":
                self.error = data
            elif kind == "download_error":
                self.download_error = data
            elif kind == "downloaded":
                key, tracks = data
                self.model.downloaded.setdefault(key, set()).update(tracks)
                self.message = f"{len(tracks)} download(s) finished. Checking the library automatically…"
            elif kind == "failed":
                key, tracks = data
                for history in (self.model.queued, self.model.matched, self.model.downloaded):
                    history.get(key, set()).difference_update(tracks)
                release = self.model.releases.get(key, {})
                self.last_result = (f"{release.get('title', 'Release')}: {len(tracks)} transfer(s) failed; "
                                    "press d or t to retry.")
                self.message = self.last_result
            elif kind == "result":
                old_key, release, result = data
                if self.scanning:
                    self.refreshed_during_scan.add(release_key(release))
                self._update_release(release, old_key)
                self.model.record_result(release, result)
                unresolved = result.get("total_missing", 0) - result.get("resolved_count", 0)
                count = result.get("matched_count", 0) if result.get("dry_run") else result.get("queued_count", 0)
                action = "matched (preview; nothing queued)" if result.get("dry_run") else "queued in slskd"
                self.last_result = f"{release['title']}: {count} {action}; {unresolved} unmatched."
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
                    if data == "download":
                        self.active_download = None
                    self.busy = self.scanning
                    if self.scanning:
                        self.operation = "scan"
                    if data == "refresh":
                        self.message = "Release refreshed from the library and MusicBrainz."
        self._start_next_download()

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
        if any(release_key(request.release) == release_key(release) and request.dry_run == dry_run
               and (request.only_track is None or request.only_track == only_track) for request in requests):
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

    def _handle_overlay_key(self, key, curses):
        if key in ("q", "Q", "\x03"):
            self._quit()
        elif key in ("\x1b", "?"):
            self.overlay = None
        elif self.overlay == "help":
            if key in ("\n", "\r", curses.KEY_ENTER):
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
            if self.active_download and release_key(self.active_download.release) == release_key(release):
                request_label = "[searching] "
            elif any(release_key(request.release) == release_key(release) for request in self.pending_downloads):
                request_label = "[waiting] "
            put(y, 1, f"{'›' if active else ' '} {request_label}{release['title']}", split - 2,
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
                if status in ("missing", "unverified"):
                    attr |= curses.A_BOLD
                credit = track.get("artist")
                title = track.get("title", "")
                if credit and credit != selected.get("artist"):
                    title += f" — {credit}"
                marker = "›" if index == self.model.track_index else " "
                put(5 + index - track_start, split + 2, f"{marker}{label:>5} {status.upper():<10} {title}", attr=attr)
            detail = selected.get("audit_error") or (
                f"Track {self.model.track_index + 1}/{len(tracks)} · MBID: {selected.get('mb_release_id') or 'unresolved'}"
            )
            put(height - 6, split + 2, detail, attr=curses.A_DIM)
        put(height - 5, 1, self.error or self.download_error or self.message,
            attr=curses.A_BOLD if self.error or self.download_error else 0)
        put(height - 4, 1, self.last_result or "Downloads update automatically; DOWNLOADED = waiting for library.", attr=curses.A_DIM)
        put(height - 3, 1, "d Download all missing · t Download selected track · Enter options", attr=curses.A_BOLD)
        put(height - 2, 1, "↑↓ move · Tab pane · / artist · p preview · r refresh · ? help · q quit")
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
                "r: refresh selected release from MusicBrainz",
                "R: rescan library, reuse saved audits if unchanged",
                "d/t requests wait in order; q discards waiting requests",
                "Esc / Enter: close help    q: quit",
            ], 1):
                line(row, text)
            return

        release = self.model.selected()
        line(0, "DOWNLOAD OPTIONS" + (" · DRY RUN" if self.args.dry_run else ""), True)
        if not release:
            line(2, "No incomplete release selected. Esc to close.")
            return
        line(1, f"Release: {release['artist']} — {release['title']}")
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
