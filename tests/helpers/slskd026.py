"""In-memory slskd 0.26 REST server for exercising SlskdClient without a network.

It reproduces the server behaviour the client depends on (slskd 0.26 with
Soulseek.NET 10, see SearchService.StartAsync, SearchInternal, SearchesController,
UsersController and TransfersController):

- POST /searches returns at once. A search runs until no response has arrived
  for searchTimeout ms (the timer restarts on every response), or until
  responseLimit / fileLimit is reached.
- Both GET endpoints return no responses while a search runs. slskd marks the
  record Completed shortly before it stores the responses and endedAt.
- PUT /searches/{id} stops a running search and stores the responses so far.
- Rows left InProgress by a slskd restart (zombies) never finish, cannot be
  stopped and never return responses.
- Overlapping POSTs to /searches or /transfers/downloads/{user} get HTTP 429.
- POST /users/{user}/directory returns [{name, fileCount, files}] with basenames,
  including any subdirectories of the requested directory.
- A search waiting for one of slskd's two slots shows as Queued; stopping it
  then completes it as 'Completed, Cancelled' without responses.
- A search can end 'Completed, Errored' (e.g. the server connection dropped),
  keeping the responses received until then.
- POST /transfers/downloads/{user} returns 201 {enqueued: [...], failed: [...]}.
  failed holds (Filename, Message) value tuples, which System.Text.Json writes
  without fields as {} ('opaque', the default); 'tuple' ({item1, item2}) and
  'named' ({filename, message}) model serializers that keep them.
- DELETE /transfers/downloads/{user}/{id} cancels a transfer and returns 204:
  one that has not completed ends 'Completed, Cancelled'; a completed record
  keeps its state. remove=true also drops the record; an unknown ID is a 404.

Time is simulated: install() replaces time.monotonic, time.time and time.sleep
in discogseek.clients.slskd, and every sleep advances the fake clock.
"""

import copy
import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

EPOCH = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)


def iso(moment: datetime) -> str:
    """Format a timestamp the way slskd does: 7 fractional digits and 'Z'."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "0Z"


def peer(username: str, *filenames: str, size: int = 1000) -> Dict[str, Any]:
    """A search response from one peer, shaped like slskd's Response record."""
    return {
        "username": username, "hasFreeUploadSlot": True, "queueLength": 0, "uploadSpeed": 1_000_000,
        "fileCount": len(filenames), "lockedFileCount": 0, "lockedFiles": [],
        "files": [{"filename": name, "size": size, "extension": name.rsplit(".", 1)[-1], "isLocked": False}
                  for name in filenames],
    }


class FakeResponse:
    def __init__(self, status_code: int, body: Any = None, text: Optional[str] = None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else ("" if body is None else json.dumps(body))

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("Response has no JSON body")
        return copy.deepcopy(self._body)


class FakeSearch:
    """One search record. Arrival times are seconds after the search started."""

    def __init__(self, search_id: str, text: str, started: float, responses: Sequence[Dict[str, Any]],
                 arrivals: Optional[Sequence[float]], options: Dict[str, Any], finalize_lag: float):
        self.id = search_id
        self.text = text
        self.started = started
        self.responses = list(responses)
        self.arrivals = list(arrivals) if arrivals is not None else [1.0] * len(self.responses)
        self.timeout = options.get("searchTimeout", 15000) / 1000
        self.response_limit = options.get("responseLimit", 250)
        self.file_limit = options.get("fileLimit", 25000)
        self.finalize_lag = finalize_lag
        self.stopped_at: Optional[float] = None
        self.zombie = False
        self.queued = False  # waits for a slot until stopped
        self.errored_at: Optional[float] = None  # seconds after start
        self.fixed: Optional[Dict[str, Any]] = None  # a seeded history row

    def timeline(self) -> Tuple[float, str, List[Tuple[float, Dict[str, Any]]]]:
        """(seconds until completion, terminal state, responses kept with arrival times)."""
        if self.queued and self.stopped_at is not None:
            return max(0.0, self.stopped_at - self.started), "Cancelled", []
        kept: List[Tuple[float, Dict[str, Any]]] = []
        files = 0
        last = 0.0
        end, state = None, "TimedOut"
        for arrival, response in sorted(zip(self.arrivals, self.responses), key=lambda item: item[0]):
            if arrival - last > self.timeout:
                break
            kept.append((arrival, response))
            files += len(response.get("files", []))
            last = arrival
            if len(kept) >= self.response_limit:
                end, state = arrival, "ResponseLimitReached"
                break
            if files >= self.file_limit:
                end, state = arrival, "FileLimitReached"
                break
        if end is None:
            end = last + self.timeout
        if self.errored_at is not None and self.errored_at < end:
            end, state = self.errored_at, "Errored"
            kept = [(arrival, response) for arrival, response in kept if arrival <= end]
        if self.stopped_at is not None and self.stopped_at - self.started < end:
            end, state = max(0.0, self.stopped_at - self.started), "Cancelled"
            kept = [(arrival, response) for arrival, response in kept if arrival <= end]
        return end, state, kept


class FakeSlskd026:
    """The slskd REST surface SlskdClient uses; install() routes the client to it.

    finalize_lag is how long a finished search shows as Completed before slskd
    stores its responses and endedAt.
    """

    def __init__(self, finalize_lag: float = 0.5):
        self.now = 0.0
        self.finalize_lag = finalize_lag
        self.searches: Dict[str, FakeSearch] = {}
        self.plans: Dict[str, Tuple[List[Dict[str, Any]], Optional[List[float]]]] = {}
        self.search_post_errors: Dict[str, List[Tuple[int, str]]] = {}
        self.busy_search_posts = 0
        self.busy_enqueue_posts = 0
        self.post_hold = 0.0  # real seconds a POST holds slskd's request limiter
        self.enqueue_entered = threading.Event()  # set once an enqueue holds the limiter
        self.directories: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        self.enqueue_errors: Dict[str, str] = {}
        self.enqueue_user_errors: Dict[str, List[Tuple[int, str]]] = {}
        self.failed_entry_style = "opaque"
        self.queue_new_searches = False  # other clients hold both search slots
        self.errored_searches: Dict[str, float] = {}  # text -> seconds until the search errors
        self.transfers: List[Dict[str, Any]] = []
        self.cancelled: List[Tuple[str, str]] = []  # (user, transfer ID) of each accepted DELETE
        self.requests: List[Tuple[str, str, Any]] = []
        self.request_times: List[Tuple[float, str, str]] = []  # (fake time, method, path)
        self.peak_running = 0
        self._lock = threading.RLock()
        self._search_limiter = threading.Lock()
        self._enqueue_limiter = threading.Lock()
        self._next_id = 0

    # --- simulation control -------------------------------------------------

    @property
    def wall(self) -> datetime:
        return EPOCH + timedelta(seconds=self.now)

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.now += max(0.0, float(seconds))

    def install(self, monkeypatch, client) -> "FakeSlskd026":
        monkeypatch.setattr(client, "_request", self.request)
        monkeypatch.setattr("discogseek.clients.slskd.time.monotonic", lambda: self.now)
        monkeypatch.setattr("discogseek.clients.slskd.time.time", lambda: self.wall.timestamp())
        monkeypatch.setattr("discogseek.clients.slskd.time.sleep", self.sleep)
        return self

    def plan_search(self, text: str, responses: Sequence[Dict[str, Any]],
                    arrivals: Optional[Sequence[float]] = None) -> None:
        """Responses the network returns for a future search of text."""
        self.plans[text.casefold()] = (list(responses), list(arrivals) if arrivals is not None else None)

    def add_history(self, text: str, state: str = "Completed, TimedOut", age: float = 60.0,
                    duration: float = 20.0, responses: Sequence[Dict[str, Any]] = (),
                    response_count: Optional[int] = None, file_count: Optional[int] = None) -> str:
        """Seed slskd's history with a finished row, or an InProgress zombie, started age seconds ago."""
        with self._lock:
            search = self._new_search(text, self.now - age, [], None, {})
            responses = list(responses)
            complete = "Completed" in state
            started = self.wall - timedelta(seconds=age)
            search.zombie = not complete
            search.fixed = {
                "id": search.id, "searchText": text, "token": 1000 + self._next_id, "state": state,
                "isComplete": complete, "startedAt": iso(started),
                "endedAt": iso(started + timedelta(seconds=duration)) if complete else None,
                "responseCount": len(responses) if response_count is None else response_count,
                "fileCount": (sum(len(response.get("files", [])) for response in responses)
                              if file_count is None else file_count),
                "lockedFileCount": 0,
                "responses": responses,
            }
            return search.id

    def add_running(self, text: str, age: float, responses: Sequence[Dict[str, Any]] = (),
                    arrivals: Optional[Sequence[float]] = None) -> str:
        """Seed a live search (e.g. another client's) that started age seconds ago."""
        with self._lock:
            return self._new_search(text, self.now - age, responses, arrivals, {}).id

    def add_directory(self, user: str, name: str, *basenames: str, size: int = 1000) -> None:
        self.directories[(user, name)] = [{"filename": base, "size": size, "code": 1,
                                           "extension": base.rsplit(".", 1)[-1]} for base in basenames]

    def calls(self, method: str, prefix: str = "") -> List[Tuple[str, str, Any]]:
        return [call for call in self.requests if call[0] == method and call[1].startswith(prefix)]

    # --- REST surface ---------------------------------------------------------

    def request(self, method: str, path: str, **kwargs) -> FakeResponse:
        body = kwargs.get("json")
        with self._lock:
            self.requests.append((method, path, copy.deepcopy(body)))
            self.request_times.append((self.now, method, path))
        parts = urlsplit(path)
        segments = [unquote(part) for part in parts.path.split("/") if part][2:]  # drop api/v0
        query = parse_qs(parts.query)
        if segments[:1] == ["searches"]:
            return self._searches(method, segments[1:], query, body)
        if segments[:1] == ["users"] and segments[2:] == ["directory"] and method == "POST":
            return self._browse(segments[1], body)
        if segments[:2] == ["transfers", "downloads"]:
            if method == "GET" and len(segments) == 2:
                return FakeResponse(200, self._downloads())
            if method == "POST" and len(segments) == 3:
                return self._enqueue(segments[2], body)
            if method == "DELETE" and len(segments) == 4:
                remove = (kwargs.get("params") or {}).get("remove", (query.get("remove") or ["false"])[0])
                return self._cancel(segments[2], segments[3], str(remove).lower() == "true")
        return FakeResponse(404, text="Not found")

    def _searches(self, method: str, rest: List[str], query: Dict[str, List[str]], body: Any) -> FakeResponse:
        if method == "POST" and not rest:
            return self._post_search(body)
        if method == "GET" and not rest:
            with self._lock:
                return FakeResponse(200, [dict(self._record(search), responses=[])
                                          for search in self.searches.values()])
        with self._lock:
            search = self.searches.get(rest[0]) if rest else None
            if search is None:
                return FakeResponse(404, text="Not found")
            if method == "GET":
                record = self._record(search)
                if rest[1:] == ["responses"]:
                    return FakeResponse(200, record["responses"])
                if query.get("includeResponses") != ["true"]:
                    record["responses"] = []
                return FakeResponse(200, record)
            if method == "PUT":
                complete = self._record(search)["isComplete"]
                if not search.zombie and not complete and search.stopped_at is None:
                    search.stopped_at = self.now
                return FakeResponse(200)
            if method == "DELETE":
                del self.searches[search.id]
                return FakeResponse(204)
        return FakeResponse(405, text="Method not allowed")

    def _post_search(self, body: Dict[str, Any]) -> FakeResponse:
        if not self._search_limiter.acquire(blocking=False):
            return FakeResponse(429, text="Only one concurrent operation is permitted. Wait until the previous request completes")
        try:
            threading.Event().wait(self.post_hold)
            with self._lock:
                if self.busy_search_posts:
                    self.busy_search_posts -= 1
                    return FakeResponse(429, text="Only one concurrent operation is permitted. Wait until the previous request completes")
                text = body["searchText"]
                errors = self.search_post_errors.get(text.casefold())
                if errors:
                    status, message = errors.pop(0)
                    return FakeResponse(status, text=message)
                responses, arrivals = self.plans.get(text.casefold(), ([], None))
                search = self._new_search(text, self.now, responses, arrivals, body)
                search.queued = self.queue_new_searches
                search.errored_at = self.errored_searches.get(text.casefold())
                running = sum(1 for other in self.searches.values()
                              if not other.zombie and not self._record(other)["isComplete"])
                self.peak_running = max(self.peak_running, running)
                record = self._record(search)
                record["state"] = "Requested"
                return FakeResponse(200, record)
        finally:
            self._search_limiter.release()

    def _new_search(self, text: str, started: float, responses: Sequence[Dict[str, Any]],
                    arrivals: Optional[Sequence[float]], options: Dict[str, Any]) -> FakeSearch:
        self._next_id += 1
        search_id = f"00000000-0000-4000-8000-{self._next_id:012d}"
        search = FakeSearch(search_id, text, started, responses, arrivals, options, self.finalize_lag)
        self.searches[search_id] = search
        return search

    def _record(self, search: FakeSearch) -> Dict[str, Any]:
        if search.fixed is not None:
            return copy.deepcopy(search.fixed if search.fixed["isComplete"] else dict(search.fixed, responses=[]))
        if search.queued and search.stopped_at is None:
            return {
                "id": search.id, "searchText": search.text, "token": 1000 + int(search.id[-6:]),
                "startedAt": iso(EPOCH + timedelta(seconds=search.started)), "endedAt": None,
                "state": "Queued", "isComplete": False, "responseCount": 0, "fileCount": 0,
                "lockedFileCount": 0, "responses": [],
            }
        end, state, kept = search.timeline()
        elapsed = self.now - search.started
        started = EPOCH + timedelta(seconds=search.started)
        record = {
            "id": search.id, "searchText": search.text, "token": 1000 + int(search.id[-6:]),
            "startedAt": iso(started), "endedAt": None, "lockedFileCount": 0, "responses": [],
        }
        if elapsed < end:
            arrived = [response for arrival, response in kept if arrival <= elapsed]
            record.update(state="InProgress", isComplete=False)
        else:
            arrived = [response for _, response in kept]
            record.update(state=f"Completed, {state}", isComplete=True)
            if elapsed >= end + search.finalize_lag:
                record.update(endedAt=iso(started + timedelta(seconds=end + search.finalize_lag)),
                              responses=copy.deepcopy(arrived))
        record["responseCount"] = len(arrived)
        record["fileCount"] = sum(len(response.get("files", [])) for response in arrived)
        return record

    def _browse(self, user: str, body: Dict[str, Any]) -> FakeResponse:
        requested = body["directory"]
        listing = [
            {"name": name, "fileCount": len(files), "files": copy.deepcopy(files)}
            for (owner, name), files in self.directories.items()
            if owner == user and (name == requested or name.startswith(requested + "\\"))
        ]
        if not listing:
            return FakeResponse(404, text=f"User {user} appears to be offline")
        return FakeResponse(200, listing)

    def _downloads(self) -> List[Dict[str, Any]]:
        users: Dict[str, List[Dict[str, Any]]] = {}
        with self._lock:
            for transfer in self.transfers:
                users.setdefault(transfer["username"], []).append(copy.deepcopy(transfer))
        return [{"username": user, "directories": [{"directory": "", "files": files}]}
                for user, files in users.items()]

    def _cancel(self, user: str, transfer_id: str, remove: bool) -> FakeResponse:
        with self._lock:
            transfer = next((transfer for transfer in self.transfers
                             if transfer["username"] == user and transfer["id"] == transfer_id), None)
            if transfer is None:
                return FakeResponse(404, text="Not found")
            if "Completed" not in transfer["state"]:
                transfer.update(state="Completed, Cancelled", endedAt=iso(self.wall))
            if remove:
                self.transfers.remove(transfer)
            self.cancelled.append((user, transfer_id))
            return FakeResponse(204)

    def _failed_entry(self, filename: str, message: str) -> Any:
        if self.failed_entry_style == "named":
            return {"filename": filename, "message": message}
        if self.failed_entry_style == "opaque":
            return {}
        return {"item1": filename, "item2": message}

    def _enqueue(self, user: str, files: List[Dict[str, Any]]) -> FakeResponse:
        if not self._enqueue_limiter.acquire(blocking=False):
            return FakeResponse(429, text="Only one concurrent operation is permitted. Wait until the previous request completes")
        try:
            self.enqueue_entered.set()
            threading.Event().wait(self.post_hold)
            with self._lock:
                if self.busy_enqueue_posts:
                    self.busy_enqueue_posts -= 1
                    return FakeResponse(429, text="Only one concurrent operation is permitted. Wait until the previous request completes")
                errors = self.enqueue_user_errors.get(user)
                if errors:
                    status, message = errors.pop(0)
                    return FakeResponse(status, text=message)
                live: Set[str] = {transfer["filename"] for transfer in self.transfers
                                  if transfer["username"] == user and "Completed" not in transfer["state"]}
                enqueued, failed = [], []
                for file in files:
                    filename = file["filename"]
                    if filename in live:
                        failed.append(self._failed_entry(filename, "Skipped: Already in progress"))
                    elif filename in self.enqueue_errors:
                        failed.append(self._failed_entry(filename, f"Error: {self.enqueue_errors[filename]}"))
                    else:
                        self._next_id += 1
                        transfer = {
                            "id": f"transfer-{self._next_id}", "username": user, "direction": "Download",
                            "filename": filename, "size": file.get("size", 0), "state": "Queued, Locally",
                            "requestedAt": iso(self.wall),
                        }
                        self.transfers.append(transfer)
                        enqueued.append(copy.deepcopy(transfer))
                return FakeResponse(201, {"enqueued": enqueued, "failed": failed})
        finally:
            self._enqueue_limiter.release()
