"""
Robust Python client for interacting with slskd's REST API (v0).
"""

import logging
import os
import re
import time
import urllib.parse
import threading
from collections import OrderedDict, deque
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any, Union, Tuple, Set, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from discogseek.config import Config
from discogseek.clients.http import create_resilient_session
from discogseek.core.peer_policy import PeerPolicy, peer_key
from discogseek.core.transfers import DownloadHistory, transfer_states

MAX_DIRECTORY_CACHE_SIZE: int = 500


def _env_number(name: str, default: float, minimum: float = 1.0) -> float:
    try:
        value = float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
    return value if value >= minimum else default


# slskd 0.26 (Soulseek.NET 10) search semantics this client is built around:
# - searchTimeout is an inactivity timeout that restarts on every response, so
#   productive searches routinely run 30-67 s instead of 15 s.
# - Responses are stored, and returned by either GET endpoint, only once the
#   search has finished. PUT /searches/{id} stops a search and stores the
#   responses received so far.
# - At most two searches run at once; later ones wait in slskd's queue.
SEARCH_TIMEOUT_MS: int = 15000
SEARCH_RESPONSE_LIMIT: int = int(_env_number("DISCOGSEEK_SEARCH_RESPONSE_LIMIT", 500))
SEARCH_FILE_LIMIT: int = int(_env_number("DISCOGSEEK_SEARCH_FILE_LIMIT", 50000))
MAX_CONCURRENT_SEARCHES: int = 2
# A search still running after max(timeout, SEARCH_MIN_WAIT) + SEARCH_GRACE_PERIOD
# seconds is stopped, then given SEARCH_FINALIZE_WAIT seconds to store its results.
SEARCH_MIN_WAIT: float = 28.0
SEARCH_GRACE_PERIOD: float = 45.0
SEARCH_FINALIZE_WAIT: float = 10.0
# Completed searches are reused only while their peer snapshot is this recent.
SEARCH_REUSE_MAX_AGE: float = 60.0 * _env_number("DISCOGSEEK_SEARCH_REUSE_MINUTES", 60, minimum=0.0)
# A search that completed normally without files is reused for a shorter time
# (and never longer than SEARCH_REUSE_MAX_AGE): nobody shared the query then.
SEARCH_EMPTY_REUSE_MAX_AGE: float = 60.0 * _env_number("DISCOGSEEK_EMPTY_SEARCH_REUSE_MINUTES", 15, minimum=0.0)
# Rows still InProgress after this long were abandoned by a slskd restart.
SEARCH_ABANDONED_AGE: float = 300.0
# slskd accepts one enqueue request at a time and answers others with HTTP 429.
ENQUEUE_BUSY_DELAYS: Tuple[float, ...] = (1.0, 2.0, 4.0)
# slskd reports files that already have a live transfer as failed "Skipped" entries.
ENQUEUE_SKIP_MARKERS: Tuple[str, ...] = ("skipped", "already in progress", "already queued", "already enqueued")

_TIMESTAMP = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?\s*(Z|[+-]\d{2}:?\d{2})?$", re.IGNORECASE,
)


def _parse_timestamp(value: Any) -> Optional[float]:
    """Parse an slskd timestamp (7 fractional digits and 'Z') to epoch seconds."""
    match = _TIMESTAMP.match(value.strip()) if isinstance(value, str) else None
    if not match:
        return None
    day, clock, fraction, zone = match.groups()
    try:
        moment = datetime.strptime(f"{day}T{clock}", "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    offset = timedelta(0)
    if zone and zone.upper() != "Z":
        digits = zone[1:].replace(":", "")
        offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
        if zone[0] == "-":
            offset = -offset
    seconds = moment.replace(tzinfo=timezone(offset)).timestamp()
    return seconds + float(f"0.{fraction}") if fraction else seconds


def _search_state(data: Dict[str, Any]) -> str:
    return str(data.get("state") or "")


def _search_complete(data: Dict[str, Any]) -> bool:
    return bool(data.get("isComplete")) or "Completed" in _search_state(data)


def _search_finalized(data: Dict[str, Any]) -> bool:
    # slskd marks a search Completed before it stores the responses, so a
    # completed record that counted responses but returns none is not final.
    return _search_complete(data) and bool(data.get("responses") or not data.get("responseCount"))


def _search_found_nothing(data: Dict[str, Any]) -> bool:
    # Searched the network to its normal end, and nobody answered with files.
    state = _search_state(data)
    return (_search_complete(data) and not data.get("responses") and not data.get("responseCount")
            and not data.get("fileCount") and "Errored" not in state and "Cancelled" not in state)


def _any_files(rows: List[Dict[str, Any]]) -> bool:
    return any((row.get("fileCount") or 0) > 0 for row in rows)


def _search_hard_cap(timeout: float) -> float:
    return max(float(timeout or 0.0), SEARCH_MIN_WAIT) + SEARCH_GRACE_PERIOD


def _search_failure(query: str, error: Union[Exception, str]) -> Dict[str, Any]:
    """The batch_search entry of a query that was not searched or not read."""
    return {"error": str(error) or type(error).__name__, "responses": [], "searchText": query}


def _flatten_directory_listing(body: Any, directory: str) -> List[Dict[str, Any]]:
    """Return a browse reply as files whose 'filename' is the full remote path.

    slskd 0.26 answers with Soulseek directories, [{name, fileCount, files}],
    whose files carry only basenames. Subdirectories the peer includes are
    dropped when the requested directory is present. Entries that are already
    files with a path are kept as they are.
    """
    if isinstance(body, dict):
        body = [body]
    if not isinstance(body, list):
        return []

    def _key(name: Any) -> str:
        return str(name or "").replace("/", "\\").rstrip("\\").casefold()

    directories = [entry for entry in body if isinstance(entry, dict) and isinstance(entry.get("files"), list)]
    requested = [entry for entry in directories if _key(entry.get("name")) == _key(directory)]
    files: List[Dict[str, Any]] = []
    for entry in body:
        if not isinstance(entry, dict):
            continue
        if not isinstance(entry.get("files"), list):
            if entry.get("filename"):
                files.append(entry)
            continue
        if requested and not any(entry is match for match in requested):
            continue
        prefix = str(entry.get("name") or directory or "").rstrip("\\/")
        separator = "/" if "/" in prefix and "\\" not in prefix else "\\"
        for item in entry["files"]:
            if not isinstance(item, dict) or not item.get("filename"):
                continue
            filename = str(item["filename"])
            if prefix and "\\" not in filename and "/" not in filename:
                filename = f"{prefix}{separator}{filename}"
            files.append({**item, "filename": filename})
    return files


def _enqueue_failure_entry(entry: Any) -> Tuple[Optional[str], str]:
    """Read one 'failed' entry of an enqueue reply as (filename, message).

    These are (Filename, Message) tuples, which serializers write as {item1,
    item2} or, like slskd 0.26's System.Text.Json, as {} with neither value.
    Other spellings are accepted too.
    """
    filename: Any = None
    message: Any = ""
    if isinstance(entry, dict):
        fields = {str(key).casefold(): value for key, value in entry.items()}
        filename = fields.get("filename") or fields.get("item1")
        message = fields.get("message") or fields.get("item2") or fields.get("error") or ""
    elif isinstance(entry, (list, tuple)):
        filename = entry[0] if entry else None
        message = entry[1] if len(entry) > 1 else ""
    else:
        message = entry
    return (filename if isinstance(filename, str) and filename else None), str(message or "")


def _split_enqueue_result(
    resp: Any, chunk: List[Dict[str, Any]], live_filenames: Callable[[], Set[str]] = set,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Split a successful enqueue reply, {enqueued, failed}, into accepted files and failures.

    Files that already have a live transfer are reported as failed "Skipped"
    entries; they are accepted, because slskd is already downloading them.
    slskd 0.26 serializes the failed (Filename, Message) tuples without their
    fields ({}). A file that is neither named as enqueued nor identified as
    rejected is then accepted only if live_filenames() shows a live transfer.
    """
    try:
        body = resp.json()
    except Exception:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("failed"), list) or not body["failed"]:
        return list(chunk), []

    names_in_chunk = {file["filename"] for file in chunk}
    skipped: Set[str] = set()
    rejected: Dict[str, str] = {}
    unidentified: List[str] = []
    for entry in body["failed"]:
        filename, message = _enqueue_failure_entry(entry)
        if filename is None and message in names_in_chunk:
            filename, message = message, ""
        if any(marker in message.casefold() for marker in ENQUEUE_SKIP_MARKERS):
            if filename in names_in_chunk:
                skipped.add(filename)
            continue
        if filename in names_in_chunk:
            rejected[filename] = message or "rejected"
        else:
            unidentified.append(message or "unknown error")
    if not rejected and not unidentified:
        return list(chunk), []

    failures = [f"{name}: {message}" for name, message in rejected.items()]
    if not unidentified:
        return [file for file in chunk if file["filename"] not in rejected], failures

    enqueued = body.get("enqueued")
    accepted_names = set(skipped)
    if isinstance(enqueued, list):
        accepted_names.update(transfer.get("filename") for transfer in enqueued if isinstance(transfer, dict))
    unresolved = names_in_chunk - accepted_names - set(rejected)
    if unresolved:
        try:
            live = set(live_filenames())
        except Exception:
            live = set()
        accepted_names.update(unresolved & live)
        unresolved -= live
    if unresolved:
        failures += [f"{file['filename']}: not queued" for file in chunk if file["filename"] in unresolved]
        failures += [message for message in dict.fromkeys(unidentified) if message != "unknown error"]
    return [file for file in chunk if file["filename"] in accepted_names], failures


class _ActiveSearch:
    """Client-side progress of one slskd search until it has a final outcome."""

    __slots__ = ("query", "search_id", "hard_cap", "submitted", "started", "completed_at", "stop_requested", "data", "error")

    def __init__(self, query: str, search_id: str, hard_cap: float, now: float, age: float = 0.0):
        self.query = query
        self.search_id = search_id
        self.hard_cap = hard_cap
        self.submitted = now
        # Attached searches count their time limit from slskd's start time.
        self.started = now - max(0.0, age)
        self.completed_at: Optional[float] = None
        self.stop_requested: Optional[float] = None
        self.data: Dict[str, Any] = {"id": search_id, "searchText": query, "responses": []}
        self.error: Optional[str] = None


class SlskdAPIError(Exception):
    """Base exception for slskd API errors."""
    pass


class SlskdSearchNotFoundError(SlskdAPIError):
    """slskd no longer has the search record (deleted or pruned)."""


class SlskdEnqueueError(SlskdAPIError):
    """A failed submission, retaining any earlier chunks accepted by slskd."""

    def __init__(self, message: str, queued_files: Optional[List[Dict[str, Any]]] = None):
        super().__init__(message)
        self.queued_files = list(queued_files or [])


class SlskdEnqueueRefusedError(SlskdEnqueueError):
    """slskd did not queue the files missing from queued_files, so another source may supply them.

    Raised when slskd's reply names files it did not queue, when it stays busy
    (HTTP 429) or refuses the request (HTTP 4xx), and when the request was never
    sent. A lost reply or a server error leaves it unknown whether files were queued.
    """


class SlskdPeerUnavailableError(SlskdEnqueueError):
    """slskd could not contact a peer; another source may still work."""

    def __init__(
        self, username: str, reason: str,
        queued_files: Optional[List[Dict[str, Any]]] = None,
    ):
        self.username = username
        self.reason = reason
        detail = "is offline" if reason == "offline" else "could not be reached after 3 attempts"
        super().__init__(f"Soulseek user {username} {detail}.", queued_files)


class SlskdTransferFailedError(SlskdEnqueueError):
    """A previously failed file must be obtained from another source."""


class SlskdPeerBlockedError(SlskdEnqueueError):
    """A persistent peer block prevents new downloads from this source."""

    def __init__(self, username: str, reason: str, queued_files=None):
        self.username = username
        self.reason = reason
        super().__init__(f"Soulseek user {username} is blocked: {reason}.", queued_files)


class SlskdClient:
    """Client for communicating with the slskd REST API."""

    MAX_DIRECTORY_CACHE_SIZE: int = MAX_DIRECTORY_CACHE_SIZE

    def __init__(
        self,
        base_url: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 15.0,
    ):
        self.base_url = (base_url or Config.SLSKD_URL).rstrip("/")
        self.username = username or Config.SLSKD_USERNAME
        self.password = password or Config.SLSKD_PASSWORD
        self.api_key = api_key or Config.SLSKD_API_KEY
        self.timeout = timeout
        self.peer_policy = PeerPolicy()

        self.session = create_resilient_session()
        self.token: Optional[str] = None
        self.token_expiry: float = 0
        self._lock = threading.Lock()
        self._search_lock = threading.Lock()

        # Bounded LRU memory cache for directory browsing
        self._max_cache_size: int = MAX_DIRECTORY_CACHE_SIZE
        self._directory_cache: OrderedDict[str, List[Dict[str, Any]]] = OrderedDict()

        self._ensure_authenticated()

    @property
    def max_directory_cache_size(self) -> int:
        return self._max_cache_size

    @max_directory_cache_size.setter
    def max_directory_cache_size(self, value: int) -> None:
        self._max_cache_size = value

    def _ensure_authenticated(self, force_refresh: bool = False) -> None:
        """Ensures an active, valid authentication token or API key header."""
        if self.api_key:
            self.session.headers.update({"X-API-Key": self.api_key})
            return

        now = time.time()
        if not force_refresh and self.token and now < self.token_expiry - 60:
            return

        with self._lock:
            if not force_refresh and self.token and now < self.token_expiry - 60:
                return

            if not self.username or not self.password:
                try:
                    resp = self.session.get(f"{self.base_url}/api/v0/application", timeout=self.timeout)
                    if resp.status_code == 200:
                        return
                except Exception:
                    pass
                raise SlskdAPIError("No slskd credentials found. Please set SLSKD_USERNAME and SLSKD_PASSWORD or SLSKD_API_KEY in .env.")

            session_url = f"{self.base_url}/api/v0/session"
            try:
                resp = self.session.post(
                    session_url,
                    json={"username": self.username, "password": self.password},
                    timeout=self.timeout
                )
                if resp.status_code != 200:
                    raise SlskdAPIError(f"Authentication failed (HTTP {resp.status_code}): {resp.text}")

                data = resp.json()
                self.token = data.get("token")
                self.token_expiry = data.get("expires", now + 3600)
                self.session.headers.update({"Authorization": f"Bearer {self.token}"})
            except requests.RequestException as e:
                raise SlskdAPIError(f"Failed to connect to slskd at {self.base_url}: {e}")

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """Performs an authenticated request with automatic token retry on 401."""
        self._ensure_authenticated()
        url = f"{self.base_url}{path}"
        if "timeout" not in kwargs:
            kwargs["timeout"] = self.timeout

        try:
            resp = self.session.request(method, url, **kwargs)
            if resp.status_code == 401 and not self.api_key:
                self._ensure_authenticated(force_refresh=True)
                resp = self.session.request(method, url, **kwargs)
            return resp
        except requests.RequestException as e:
            raise SlskdAPIError(f"slskd request error [{method} {path}]: {e}")

    def get_application(self) -> Dict[str, Any]:
        """Gets slskd system state, connected user, and Soulseek server status."""
        resp = self._request("GET", "/api/v0/application")
        if resp.status_code != 200:
            raise SlskdAPIError(f"Failed to get application info (HTTP {resp.status_code}): {resp.text}")
        return resp.json()

    def get_options(self) -> Dict[str, Any]:
        """Gets slskd configuration and storage directories."""
        resp = self._request("GET", "/api/v0/options")
        if resp.status_code != 200:
            raise SlskdAPIError(f"Failed to get options (HTTP {resp.status_code}): {resp.text}")
        return resp.json()

    def list_searches(self) -> List[Dict[str, Any]]:
        """Lists all searches stored in slskd history."""
        resp = self._request("GET", "/api/v0/searches")
        if resp.status_code != 200:
            return []
        return resp.json()

    def _start_search(self, query: str) -> Dict[str, Any]:
        # slskd rejects overlapping POSTs, even though searches run concurrently.
        body = {
            "searchText": query,
            "searchTimeout": SEARCH_TIMEOUT_MS,
            "responseLimit": SEARCH_RESPONSE_LIMIT,
            "fileLimit": SEARCH_FILE_LIMIT,
        }
        with self._search_lock:
            for attempt in range(3):
                resp = self._request("POST", "/api/v0/searches", json=body)
                if resp.status_code != 429 or attempt == 2:
                    break
                logging.getLogger(__name__).warning(
                    "slskd busy (HTTP 429); retrying search — %s", query,
                )
                time.sleep(attempt + 1)
            if resp.status_code not in (200, 201):
                raise SlskdAPIError(
                    f"Failed to initiate search for '{query}' (HTTP {resp.status_code}): {resp.text}"
                )
            data = resp.json()
            if not isinstance(data, dict) or not data.get("id"):
                raise SlskdAPIError(f"slskd returned no search ID for '{query}'")
            return data

    def _stop_search(self, search_id: str) -> bool:
        """Stop a running search; slskd then stores the responses received so far."""
        try:
            resp = self._request("PUT", f"/api/v0/searches/{search_id}", json={})
        except SlskdAPIError:
            return False
        return resp.status_code in (200, 204, 304)

    def _search_history(self) -> List[Dict[str, Any]]:
        try:
            rows = self.list_searches()
        except (SlskdAPIError, ValueError) as error:
            logging.getLogger(__name__).warning("Cannot read slskd search history: %s", error)
            return []
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def _classify_history(
        self, rows: List[Dict[str, Any]], query: str, hard_cap: float,
    ) -> Tuple[List[Dict[str, Any]], List[Tuple[Dict[str, Any], float]], List[Dict[str, Any]]]:
        """Split history rows for query into reusable, attachable and abandoned searches.

        Reusable rows completed normally with files within SEARCH_REUSE_MAX_AGE;
        a search stopped at the time limit counts, one stopped earlier does not.
        A search that completed normally without files is reusable within
        SEARCH_EMPTY_REUSE_MAX_AGE (never when errored or stopped).
        Attachable rows are running and younger than the search time limit, as
        (row, age in seconds). Older running rows were never finalized (slskd
        restart) and return no responses; both lists are ordered newest first.
        """
        now = time.time()
        text = query.strip().lower()
        reusable: List[Tuple[float, Dict[str, Any]]] = []
        running: List[Tuple[float, Dict[str, Any]]] = []
        abandoned: List[Dict[str, Any]] = []
        for row in rows:
            if not row.get("id") or str(row.get("searchText") or "").strip().lower() != text:
                continue
            started = _parse_timestamp(row.get("startedAt"))
            if _search_complete(row):
                ended = _parse_timestamp(row.get("endedAt"))
                ended = ended if ended is not None else started
                state = _search_state(row)
                # A search stopped (PUT) at its time limit kept everything it had
                # collected by then, like one that timed out.
                stopped_at_limit = (
                    ended is not None and started is not None and ended - started >= _search_hard_cap(0)
                )
                if (row.get("fileCount") or 0) > 0:
                    max_age = SEARCH_REUSE_MAX_AGE
                    usable = "Errored" not in state and ("Cancelled" not in state or stopped_at_limit)
                else:
                    max_age = min(SEARCH_EMPTY_REUSE_MAX_AGE, SEARCH_REUSE_MAX_AGE)
                    usable = _search_found_nothing(row)
                if ended is not None and max_age > 0 and now - ended <= max_age and usable:
                    reusable.append((ended, row))
            elif started is not None and now - started <= hard_cap:
                running.append((started, row))
            else:
                abandoned.append(row)
        reusable.sort(key=lambda item: item[0], reverse=True)
        running.sort(key=lambda item: item[0], reverse=True)
        return (
            [row for _, row in reusable],
            [(row, max(0.0, now - started)) for started, row in running],
            abandoned,
        )

    def _discard_abandoned_searches(self, rows: List[Dict[str, Any]], hard_cap: float) -> None:
        # Another client's long or queued search may still be legitimately
        # running for a while; only clearly dead rows are deleted.
        now = time.time()
        for row in rows:
            started = _parse_timestamp(row.get("startedAt"))
            if started is None or now - started < max(SEARCH_ABANDONED_AGE, 3 * hard_cap):
                continue
            try:
                self.delete_search(row["id"])
            except Exception:
                pass

    def _advance_search(
        self, active: _ActiveSearch, data: Optional[Dict[str, Any]], error: Optional[Exception],
    ) -> bool:
        """Apply one poll of an active search; return True once its outcome is final."""
        now = time.monotonic()
        logger = logging.getLogger(__name__)
        if error is not None:
            active.error = str(error) or type(error).__name__
            if isinstance(error, SlskdSearchNotFoundError):
                return True
        elif data is not None:
            active.data = data
            active.error = None
            if _search_finalized(data):
                return True
            if _search_complete(data):
                active.completed_at = now if active.completed_at is None else active.completed_at
                if now - active.completed_at >= SEARCH_FINALIZE_WAIT:
                    active.error = (
                        f"slskd completed the search with {data.get('responseCount')} responses "
                        "but did not store them"
                    )
                    return True
                return False
            state = _search_state(data)
            if "Queued" in state or "Requested" in state:
                # slskd runs two searches at once; the time limit starts when this one does.
                active.started = max(active.started, now)

        if active.stop_requested is None:
            if now - active.started >= active.hard_cap or now - active.submitted >= 2 * active.hard_cap:
                logger.warning(
                    "Soulseek search still running after %.0fs; stopping it to keep its results — %s",
                    now - active.submitted, active.query,
                )
                self._stop_search(active.search_id)
                active.stop_requested = now
            return False
        if now - active.stop_requested >= SEARCH_FINALIZE_WAIT:
            active.error = active.error or (
                f"slskd did not finish the search within {now - active.submitted:.0f}s"
            )
            return True
        return False

    @staticmethod
    def _search_outcome(active: _ActiveSearch) -> Tuple[Dict[str, Any], Optional[str]]:
        if active.data.get("responses"):
            return active.data, None
        error = active.error
        state = _search_state(active.data)
        # An errored or stopped search without responses never searched the
        # network (e.g. server disconnect, or stopped while queued in slskd):
        # it must not read as "searched, nothing found".
        if not error and "Errored" in state:
            error = "slskd reported the search as errored before any responses arrived"
        elif not error and "Cancelled" in state:
            error = "the search was stopped before any responses arrived"
        if not error:
            return active.data, None
        return _search_failure(active.query, error), error

    def search(
        self,
        query: str,
        timeout: float = 25.0,
        poll_interval: float = 1.0,
        use_existing: bool = True,
        delete_after: bool = False
    ) -> Dict[str, Any]:
        """Run one Soulseek search and return slskd's finished search record.

        Polls until slskd stores the responses. A search still running after
        max(timeout, 28) + 45 seconds is stopped, keeping its responses.
        Raises SlskdAPIError when the search cannot be submitted or read, or
        when it ends errored or stopped without any responses.
        """
        clean_q = query.strip()
        if not clean_q:
            return {"responses": [], "responseCount": 0, "fileCount": 0}

        hard_cap = _search_hard_cap(timeout)
        active: Optional[_ActiveSearch] = None
        if use_existing:
            reusable, running, abandoned = self._classify_history(self._search_history(), clean_q, hard_cap)
            self._discard_abandoned_searches(abandoned, hard_cap)
            empty = None
            for row in reusable:
                try:
                    res = self.get_search_results(row["id"])
                except Exception:
                    continue
                if res.get("responses"):
                    return res
                if empty is None and _search_found_nothing(res):
                    empty = res
            # A recent search found nothing; one still running may yet find files,
            # and a search with files that could not be read is repeated instead.
            if empty is not None and not running and not _any_files(reusable):
                return empty
            if running:
                row, age = running[0]
                active = _ActiveSearch(clean_q, row["id"], hard_cap, time.monotonic(), age)

        if active is None:
            search_data = self._start_search(clean_q)
            active = _ActiveSearch(clean_q, search_data["id"], hard_cap, time.monotonic())
            active.data = dict(search_data, responses=search_data.get("responses") or [])

        while True:
            time.sleep(poll_interval)
            try:
                data, error = self.get_search_results(active.search_id), None
            except Exception as exc:
                data, error = None, exc
            if self._advance_search(active, data, error):
                break

        result, failure = self._search_outcome(active)
        if delete_after:
            try:
                self.delete_search(active.search_id)
            except Exception:
                pass
        if failure:
            raise SlskdAPIError(f"Search for '{clean_q}' failed: {failure}")
        return result

    def batch_search(
        self,
        queries: List[str],
        timeout: float = 28.0,
        poll_interval: float = 1.0,
        max_concurrent: int = 8,
        use_existing: bool = True,
        on_progress: Optional[Callable[[int, int, str], None]] = None
    ) -> Dict[str, Dict[str, Any]]:
        """Search every query, keeping at most two slskd searches running.

        The next query starts as soon as a running one finishes. Each query maps
        to slskd's finished search record; a query whose search could not be
        submitted or read, or ended errored or stopped without any responses,
        maps to {"error": message, "responses": [], "searchText": query}.
        """
        clean_queries = list(dict.fromkeys(q.strip() for q in queries if q and q.strip()))
        if not clean_queries:
            return {}

        results: Dict[str, Dict[str, Any]] = {}
        cached_query_ids: Dict[str, List[str]] = {}
        # Queries whose only reusable searches found nothing.
        empty_only: Set[str] = set()
        # Running searches to attach to, with their start time (epoch seconds).
        running_searches: Dict[str, Tuple[str, float]] = {}
        total_queries = len(clean_queries)
        completed_queries = 0
        worker_count = max(1, min(max_concurrent, 8))
        window = min(worker_count, MAX_CONCURRENT_SEARCHES)
        hard_cap = _search_hard_cap(timeout)
        logger = logging.getLogger(__name__)

        def _complete(q_str: str, status: str, error: Optional[Union[Exception, str]] = None) -> None:
            nonlocal completed_queries
            completed_queries += 1
            logger.log(
                logging.WARNING if error else logging.INFO,
                "Soulseek searches: %s/%s %s — %s%s",
                completed_queries, total_queries, status, q_str,
                f": {error}" if error else "",
            )
            if on_progress:
                on_progress(completed_queries, total_queries, q_str)

        # Reuse recent completed searches or attach to searches still running.
        if use_existing:
            history = self._search_history()
            for query in clean_queries:
                reusable, running, abandoned = self._classify_history(history, query, hard_cap)
                self._discard_abandoned_searches(abandoned, hard_cap)
                if reusable:
                    cached_query_ids[query] = [row["id"] for row in reusable]
                    if not _any_files(reusable):
                        empty_only.add(query)
                if running:
                    # Also keep this ID if retrieving a completed cache entry fails.
                    row, age = running[0]
                    running_searches[query] = (row["id"], time.time() - age)
                if not reusable and not running:
                    for row in history:
                        if (str(row.get("searchText") or "").strip().lower() == query.lower()
                                and row.get("id") and _search_complete(row) and not row.get("fileCount")):
                            try:
                                self.delete_search(row["id"])
                            except Exception:
                                pass

        def _read_cached(search_ids: List[str], accept_empty: bool) -> Optional[Dict[str, Any]]:
            empty = None
            for search_id in search_ids:
                try:
                    data = self.get_search_results(search_id)
                except Exception:
                    continue
                if data.get("responses"):
                    return data
                if empty is None and accept_empty and _search_found_nothing(data):
                    empty = data
            return empty

        # Reuse workers for all reads so an HTTP round trip is paid once per
        # polling round instead of once per query. Progress stays on this thread.
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            # A recent search found nothing; one still running may yet find files,
            # and a search with files that could not be read is repeated instead.
            cached_futures = {
                executor.submit(_read_cached, search_ids, query in empty_only and query not in running_searches): query
                for query, search_ids in cached_query_ids.items()
            }
            for future in as_completed(cached_futures):
                query = cached_futures[future]
                data = future.result()
                if data:
                    results[query] = data
                    _complete(query, "cached")

            # A sliding window: slskd runs two searches at a time, so a third
            # would only spend its time limit in slskd's queue.
            pending = deque(query for query in clean_queries if query not in results)
            active: Dict[str, _ActiveSearch] = {}
            while pending or active:
                while pending and len(active) < window:
                    query = pending.popleft()
                    if query in running_searches:
                        search_id, started_at = running_searches[query]
                        active[query] = _ActiveSearch(
                            query, search_id, hard_cap, time.monotonic(), time.time() - started_at,
                        )
                        continue
                    try:
                        search_data = self._start_search(query)
                    except Exception as error:
                        results[query] = _search_failure(query, error)
                        _complete(query, "not submitted", error)
                        continue
                    active[query] = _ActiveSearch(query, search_data["id"], hard_cap, time.monotonic())
                if not active:
                    break

                time.sleep(poll_interval)
                poll_futures = {
                    executor.submit(self.get_search_results, search.search_id): query
                    for query, search in active.items()
                }
                for future in as_completed(poll_futures):
                    query = poll_futures[future]
                    search = active[query]
                    try:
                        data, error = future.result(), None
                    except Exception as exc:
                        data, error = None, exc
                    if not self._advance_search(search, data, error):
                        continue
                    del active[query]
                    results[query], failure = self._search_outcome(search)
                    if failure:
                        _complete(query, "results unavailable", failure)
                    elif search.stop_requested is not None:
                        _complete(query, "stopped at time limit")
                    else:
                        _complete(query, "completed")

        return results

    def get_search_results(self, search_id: str) -> Dict[str, Any]:
        """Fetches full search object with responses for a given search ID."""
        resp = self._request("GET", f"/api/v0/searches/{search_id}?includeResponses=true")
        if resp.status_code == 404:
            raise SlskdSearchNotFoundError(f"slskd has no search {search_id}")
        if resp.status_code != 200:
            raise SlskdAPIError(f"Failed to get search results for {search_id}: {resp.text}")
        data = resp.json()
        if not isinstance(data, dict):
            raise SlskdAPIError(f"slskd returned an invalid search record for {search_id}")
        # slskd stores responses only once a search finishes; /responses is a
        # fallback for builds that omit them from the search record.
        if not data.get("responses") and _search_complete(data) and data.get("responseCount") != 0:
            try:
                r_resp = self._request("GET", f"/api/v0/searches/{search_id}/responses")
                if r_resp.status_code == 200:
                    r_list = r_resp.json()
                    if r_list:
                        data["responses"] = r_list
            except Exception:
                pass
        return data

    def delete_search(self, search_id: str) -> bool:
        """Deletes a search from slskd memory."""
        resp = self._request("DELETE", f"/api/v0/searches/{search_id}")
        return resp.status_code in (200, 204)

    def browse_directory(self, username: str, directory: str, use_cache: bool = True) -> List[Dict[str, Any]]:
        """Fetches the files of a peer's directory, each with its full remote path as 'filename'."""
        cache_key = f"{username}:{directory}"
        with self._lock:
            if use_cache and cache_key in self._directory_cache:
                self._directory_cache.move_to_end(cache_key)
                return self._directory_cache[cache_key]

        try:
            encoded_user = urllib.parse.quote(username, safe="")
            resp = self._request("POST", f"/api/v0/users/{encoded_user}/directory", json={"directory": directory})
            if resp.status_code != 200:
                return []

            data = _flatten_directory_listing(resp.json(), directory)
            if use_cache:
                with self._lock:
                    self._directory_cache[cache_key] = data
                    self._directory_cache.move_to_end(cache_key)
                    while len(self._directory_cache) > self._max_cache_size:
                        self._directory_cache.popitem(last=False)
            return data
        except Exception:
            return []

    def browse_directories_batch(
        self,
        requests_list: List[Tuple[str, str]],
        use_cache: bool = True,
        max_workers: int = 6
    ) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
        """Concurrently browses multiple peer remote directories."""
        unique_reqs = list(dict.fromkeys(requests_list))
        results: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        to_fetch: List[Tuple[str, str]] = []

        for user, dir_path in unique_reqs:
            cache_key = f"{user}:{dir_path}"
            with self._lock:
                if use_cache and cache_key in self._directory_cache:
                    self._directory_cache.move_to_end(cache_key)
                    results[(user, dir_path)] = self._directory_cache[cache_key]
                    continue
            to_fetch.append((user, dir_path))

        if not to_fetch:
            return results

        def _fetch_one(u: str, d: str) -> Tuple[Tuple[str, str], List[Dict[str, Any]]]:
            res = self.browse_directory(u, d, use_cache=use_cache)
            return (u, d), res

        with ThreadPoolExecutor(max_workers=min(len(to_fetch), max_workers)) as executor:
            futures = [executor.submit(_fetch_one, u, d) for u, d in to_fetch]
            for fut in as_completed(futures):
                key, nodes = fut.result()
                results[key] = nodes

        return results

    def enqueue_download(self, username: str, files: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Enqueue files, retrying explicit peer connection failures up to 3 times.

        Offline peers fail immediately. HTTP 429 (slskd is handling another
        enqueue request) is retried after 1, 2 and 4 seconds. Other POST failures
        are not replayed: an unknown server/transport error may occur after a
        submission was accepted. When slskd accepts a request but fails some of
        its files, SlskdEnqueueError carries the accepted files as queued_files.
        SlskdEnqueueRefusedError means the other files were definitely not queued.
        """
        if not files:
            return {"status": "skipped", "count": 0}

        clean_user = re.sub(r"\s*\(.*?\)$", "", username).strip()
        encoded_user = urllib.parse.quote(clean_user, safe="")

        seen_filenames = set()
        payload = []
        for f in files:
            fn = f.get("filename")
            if fn and fn not in seen_filenames:
                seen_filenames.add(fn)
                payload.append({"filename": fn, "size": f.get("size", 0)})

        if not payload:
            return {"status": "skipped", "count": 0}

        chunk_size = 50
        queued_files = []
        logger = logging.getLogger(__name__)
        for i in range(0, len(payload), chunk_size):
            chunk = payload[i:i + chunk_size]
            attempt = 0
            busy_retries = 0
            while True:
                # A user may cancel a transfer while a long search is running.
                # Recheck history at the final submission boundary, including
                # between chunks/retries, and retain earlier accepted chunks.
                try:
                    blocked_reason = self.peer_policy.observe(self.get_downloads()).get(peer_key(clean_user))
                except Exception as error:
                    raise SlskdEnqueueRefusedError(
                        f"Cannot verify blocked peers before queueing: {error}", queued_files,
                    ) from error
                if blocked_reason:
                    raise SlskdPeerBlockedError(clean_user, blocked_reason, queued_files)
                try:
                    resp = self._request("POST", f"/api/v0/transfers/downloads/{encoded_user}", json=chunk, timeout=30.0)
                except SlskdAPIError as error:
                    raise SlskdEnqueueError(str(error), queued_files) from error
                if resp.status_code in (200, 201, 202):
                    accepted, failures = _split_enqueue_result(
                        resp, chunk, lambda: self._live_download_filenames(clean_user),
                    )
                    queued_files.extend(accepted)
                    if failures:
                        raise SlskdEnqueueRefusedError(
                            f"slskd did not queue {len(chunk) - len(accepted)} of {len(chunk)} files "
                            f"from {clean_user}: {'; '.join(failures[:3])}",
                            queued_files,
                        )
                    break

                # slskd refuses a second enqueue request while one is running,
                # before contacting the peer, so the same request is safe to resend.
                if resp.status_code == 429 and busy_retries < len(ENQUEUE_BUSY_DELAYS):
                    delay = ENQUEUE_BUSY_DELAYS[busy_retries]
                    busy_retries += 1
                    logger.info("slskd is busy queueing other downloads; retrying %s in %ss…", clean_user, delay)
                    time.sleep(delay)
                    continue

                message = resp.text.casefold()
                if 500 <= resp.status_code < 600:
                    if "appears to be offline" in message:
                        raise SlskdPeerUnavailableError(clean_user, "offline", queued_files)
                    if ("failed to connect to user" in message
                            or "failed to establish a direct or indirect message connection" in message
                            or "failed to retrieve endpoint for user" in message):
                        if attempt == 2:
                            raise SlskdPeerUnavailableError(clean_user, "unreachable", queued_files)
                        delay = 2 ** attempt
                        attempt += 1
                        logger.info(
                            "Cannot reach Soulseek user %s; retrying in %ss (attempt %s/3)…",
                            clean_user, delay, attempt + 1,
                        )
                        time.sleep(delay)
                        continue
                # slskd answers 429 and other 4xx before queueing anything.
                error_type = SlskdEnqueueRefusedError if 400 <= resp.status_code < 500 else SlskdEnqueueError
                raise error_type(
                    f"Failed to enqueue download from {clean_user} (HTTP {resp.status_code}): {resp.text}",
                    queued_files,
                )

        return {"status": "enqueued", "username": clean_user, "files_count": len(payload)}

    def get_downloads(self) -> List[Dict[str, Any]]:
        """Gets all download transfer states and queues."""
        resp = self._request("GET", "/api/v0/transfers/downloads")
        if resp.status_code != 200:
            raise SlskdAPIError(f"Failed to get downloads (HTTP {resp.status_code}): {resp.text}")
        return resp.json()

    def _live_download_filenames(self, username: str) -> Set[str]:
        """Filenames username has a transfer for that has not completed (slskd's 'already in progress').

        slskd supersedes a completed record, even a successful one, so it never makes a file skipped.
        """
        key = peer_key(username)
        live: Set[str] = set()
        for user in self.get_downloads():
            if not isinstance(user, dict) or peer_key(str(user.get("username") or "")) != key:
                continue
            for directory in user.get("directories") or []:
                for file in (directory or {}).get("files") or []:
                    if (isinstance(file, dict) and file.get("filename")
                            and "Completed" not in transfer_states(file)):
                        live.add(file["filename"])
        return live

    def cancel_download(self, username: str, transfer_id: str) -> None:
        """Stop a failed transfer's retries, retaining its record in slskd."""
        user = urllib.parse.quote(username, safe="")
        identity = urllib.parse.quote(str(transfer_id), safe="")
        # Record intent before the request: the next history read may already
        # show Cancelled, which must not turn automatic recovery into a user ban.
        self.peer_policy.record_internal_cancel(username, transfer_id)
        try:
            resp = self._request("DELETE", f"/api/v0/transfers/downloads/{user}/{identity}", params={"remove": "false"})
        except Exception:
            # An unconfirmed attempt must not exempt a later manual stop.
            self.peer_policy.forget_internal_cancel(username, transfer_id)
            raise
        if resp.status_code not in (200, 204):
            self.peer_policy.forget_internal_cancel(username, transfer_id)
            raise SlskdAPIError(
                f"Could not stop retries for {username} (HTTP {resp.status_code}): {resp.text}. "
                "No replacement was submitted."
            )

    def get_queued_filenames(self) -> Set[str]:
        """Return active, successful, or unknown transfers, excluding terminal failures."""
        return DownloadHistory(self.get_downloads()).queued_filenames

    def get_queued_track_fingerprints(self) -> Dict[str, Set[str]]:
        """Return fingerprints that protect healthy transfers from duplicate downloads."""
        full_paths = self.get_queued_filenames()
        base_filenames: Set[str] = set()
        clean_titles: Set[str] = set()

        for fn in full_paths:
            clean_p = fn.replace("/", "\\").split("\\")[-1]
            if clean_p:
                base_filenames.add(clean_p.lower())
                no_ext = os.path.splitext(clean_p)[0]
                clean_t = re.sub(r"^(\d+[\-_.]|\d+[\-_.]\d+|\d+)\s*[-_.]*\s*", "", no_ext).strip().lower()
                if " - " in clean_t:
                    clean_t = clean_t.split(" - ", 1)[1].strip()
                if clean_t:
                    clean_titles.add(clean_t)

        return {
            "full_paths": full_paths,
            "base_filenames": base_filenames,
            "clean_titles": clean_titles,
        }
