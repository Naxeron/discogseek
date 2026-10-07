"""
Robust Python client for interacting with slskd's REST API (v0).
"""

import logging
import os
import re
import time
import urllib.parse
import threading
from collections import OrderedDict
from typing import Dict, List, Optional, Any, Union, Tuple, Set, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from discogseek.config import Config
from discogseek.clients.http import create_resilient_session
from discogseek.core.peer_policy import PeerPolicy, peer_key
from discogseek.core.transfers import DownloadHistory

MAX_DIRECTORY_CACHE_SIZE: int = 500


class SlskdAPIError(Exception):
    """Base exception for slskd API errors."""
    pass


class SlskdEnqueueError(SlskdAPIError):
    """A failed submission, retaining any earlier chunks accepted by slskd."""

    def __init__(self, message: str, queued_files: Optional[List[Dict[str, Any]]] = None):
        super().__init__(message)
        self.queued_files = list(queued_files or [])


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
        with self._search_lock:
            for attempt in range(3):
                resp = self._request("POST", "/api/v0/searches", json={"searchText": query})
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

    def search(
        self,
        query: str,
        timeout: float = 25.0,
        poll_interval: float = 1.0,
        use_existing: bool = True,
        delete_after: bool = False
    ) -> Dict[str, Any]:
        """Initiates a search on the Soulseek network and polls until completion."""
        clean_q = query.strip()
        if not clean_q:
            return {"responses": [], "responseCount": 0, "fileCount": 0}

        search_id = None
        if use_existing:
            existing = self.list_searches()
            for s in reversed(existing):
                if s.get("searchText", "").strip().lower() == clean_q.lower():
                    sid = s.get("id")
                    st = s.get("state", "")
                    is_done = s.get("isComplete", False) or "Completed" in st
                    if sid and (is_done and s.get("fileCount", 0) > 0):
                        try:
                            res = self.get_search_results(sid)
                            if res.get("responses"):
                                return res
                        except Exception:
                            pass
                    elif sid and not is_done:
                        search_id = sid
                        break

        if not search_id:
            search_data = self._start_search(clean_q)
            search_id = search_data["id"]
            last_data = search_data
        else:
            last_data = {"id": search_id, "searchText": clean_q, "responses": []}

        start_time = time.time()
        effective_timeout = max(timeout, 28.0)
        while time.time() - start_time < effective_timeout:
            time.sleep(poll_interval)
            try:
                poll_resp = self._request("GET", f"/api/v0/searches/{search_id}?includeResponses=true")
                if poll_resp.status_code != 200:
                    continue

                last_data = poll_resp.json()
                is_complete = last_data.get("isComplete", False) or "Completed" in last_data.get("state", "")

                if is_complete:
                    break
            except Exception:
                pass

        # Final poll or fallback to direct /responses endpoint if includeResponses was empty
        if not last_data.get("responses") and search_id:
            try:
                r_resp = self._request("GET", f"/api/v0/searches/{search_id}/responses")
                if r_resp.status_code == 200:
                    r_list = r_resp.json()
                    if r_list:
                        last_data["responses"] = r_list
            except Exception:
                pass

        if delete_after and search_id:
            try:
                self.delete_search(search_id)
            except Exception:
                pass

        return last_data

    def batch_search(
        self,
        queries: List[str],
        timeout: float = 28.0,
        poll_interval: float = 1.0,
        max_concurrent: int = 8,
        use_existing: bool = True,
        on_progress: Optional[Callable[[int, int, str], None]] = None
    ) -> Dict[str, Dict[str, Any]]:
        """Submit searches serially in pairs, polling their results concurrently."""
        clean_queries = list(dict.fromkeys(q.strip() for q in queries if q and q.strip()))
        if not clean_queries:
            return {}

        results: Dict[str, Dict[str, Any]] = {}
        query_to_search_id: Dict[str, str] = {}
        cached_query_ids: Dict[str, str] = {}
        total_queries = len(clean_queries)
        completed_queries = 0
        worker_count = max(1, min(max_concurrent, 8))
        # slskd runs two searches at a time; larger chunks spend our timeout queued.
        chunk_size = min(worker_count, 2)
        effective_timeout = max(timeout, 28.0)
        logger = logging.getLogger(__name__)

        def _complete(q_str: str, status: str, error: Optional[Exception] = None) -> None:
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

        def _remember(q_str: str, data: Dict[str, Any]) -> None:
            previous = results.get(q_str, {})
            previous_responses = previous.get("responses") or []
            responses = data.get("responses") or []
            # slskd can return an empty/queued snapshot after streaming files.
            # Keep those files while updating the search's completion state.
            if any(r.get("files") for r in previous_responses) and not any(r.get("files") for r in responses):
                data = dict(data, responses=previous_responses)
                for field in ("fileCount", "responseCount"):
                    data[field] = max(data.get(field, 0), previous.get(field, 0))
            results[q_str] = data

        # Reuse completed searches or attach to searches already in progress.
        if use_existing:
            existing_by_text: Dict[str, List[Dict[str, Any]]] = {}
            for search in self.list_searches():
                search_text = search.get("searchText", "").strip().lower()
                if search_text and search.get("id"):
                    existing_by_text.setdefault(search_text, []).append(search)

            for query in clean_queries:
                matches = existing_by_text.get(query.lower(), [])
                if not matches:
                    continue
                running = [s for s in matches if not (
                    s.get("isComplete", False) or "Completed" in s.get("state", "")
                )]
                if running:
                    # Also keep this ID if retrieving a completed cache entry fails.
                    query_to_search_id[query] = max(running, key=lambda s: s.get("fileCount", 0))["id"]
                best = max(matches, key=lambda s: (
                    bool(s.get("fileCount", 0) > 0 and (
                        s.get("isComplete", False) or "Completed" in s.get("state", "")
                    )),
                    s.get("fileCount", 0),
                ))
                sid = best["id"]
                is_done = best.get("isComplete", False) or "Completed" in best.get("state", "")
                if is_done and best.get("fileCount", 0) > 0:
                    cached_query_ids[query] = sid
                elif not is_done:
                    query_to_search_id[query] = sid
                else:
                    try:
                        self.delete_search(sid)
                    except Exception:
                        pass

        # Reuse workers for all reads so an HTTP round trip is paid once per
        # polling round instead of once per query. Progress stays on this thread.
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            cached_futures = {
                executor.submit(self.get_search_results, sid): query
                for query, sid in cached_query_ids.items()
            }
            for future in as_completed(cached_futures):
                query = cached_futures[future]
                try:
                    data = future.result()
                    if data.get("responses"):
                        results[query] = data
                        _complete(query, "cached")
                except Exception:
                    pass

            pending_queries = [query for query in clean_queries if query not in results]
            for i in range(0, len(pending_queries), chunk_size):
                chunk = pending_queries[i:i + chunk_size]
                active_chunk: Dict[str, str] = {}
                for query in chunk:
                    try:
                        sid = query_to_search_id.get(query) or self._start_search(query)["id"]
                        active_chunk[query] = sid
                    except Exception as error:
                        _complete(query, "not submitted", error)

                start_time = time.monotonic()
                deadline = start_time + effective_timeout
                while active_chunk and time.monotonic() < deadline:
                    time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
                    poll_futures = {
                        executor.submit(self.get_search_results, sid): query
                        for query, sid in active_chunk.items()
                    }
                    for future in as_completed(poll_futures):
                        query = poll_futures[future]
                        try:
                            data = future.result()
                        except Exception:
                            continue
                        _remember(query, data)
                        is_done = data.get("isComplete", False) or "Completed" in data.get("state", "")
                        observed = query in query_to_search_id or time.monotonic() - start_time >= 6.0
                        if is_done and observed:
                            del active_chunk[query]
                            _complete(query, "completed")

                # A final concurrent read retains late responses at the deadline.
                final_futures = {
                    executor.submit(self.get_search_results, sid): query
                    for query, sid in active_chunk.items()
                }
                for future in as_completed(final_futures):
                    query = final_futures[future]
                    try:
                        _remember(query, future.result())
                    except Exception as error:
                        _complete(query, "results unavailable", error)
                        continue
                    data = results[query]
                    is_done = data.get("isComplete", False) or "Completed" in data.get("state", "")
                    _complete(query, "completed" if is_done else "still running (poll timeout)")

        return results

    def get_search_results(self, search_id: str) -> Dict[str, Any]:
        """Fetches full search object with responses for a given search ID."""
        resp = self._request("GET", f"/api/v0/searches/{search_id}?includeResponses=true")
        if resp.status_code != 200:
            raise SlskdAPIError(f"Failed to get search results for {search_id}: {resp.text}")
        data = resp.json()
        if not data.get("responses"):
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
        """Fetches complete remote file listing of a peer's directory."""
        cache_key = f"{username}:{directory}"
        with self._lock:
            if use_cache and cache_key in self._directory_cache:
                self._directory_cache.move_to_end(cache_key)
                return self._directory_cache[cache_key]

        try:
            resp = self._request("POST", f"/api/v0/users/{username}/directory", json={"directory": directory})
            if resp.status_code != 200:
                return []

            data = resp.json()
            if use_cache and data is not None:
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

        Offline peers fail immediately. Other POST failures are not replayed: an
        unknown server/transport error may occur after a submission was accepted.
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
        for i in range(0, len(payload), chunk_size):
            chunk = payload[i:i + chunk_size]
            for attempt in range(3):
                # A user may cancel a transfer while a long search is running.
                # Recheck history at the final submission boundary, including
                # between chunks/retries, and retain earlier accepted chunks.
                try:
                    blocked_reason = self.peer_policy.observe(self.get_downloads()).get(peer_key(clean_user))
                except Exception as error:
                    raise SlskdEnqueueError(
                        f"Cannot verify blocked peers before queueing: {error}", queued_files,
                    ) from error
                if blocked_reason:
                    raise SlskdPeerBlockedError(clean_user, blocked_reason, queued_files)
                try:
                    resp = self._request("POST", f"/api/v0/transfers/downloads/{encoded_user}", json=chunk, timeout=30.0)
                except SlskdAPIError as error:
                    raise SlskdEnqueueError(str(error), queued_files) from error
                if resp.status_code in (200, 201, 202):
                    queued_files.extend(chunk)
                    break

                message = resp.text.casefold()
                if 500 <= resp.status_code < 600:
                    if "appears to be offline" in message:
                        raise SlskdPeerUnavailableError(clean_user, "offline", queued_files)
                    if ("failed to connect to user" in message
                            or "failed to establish a direct or indirect message connection" in message):
                        if attempt == 2:
                            raise SlskdPeerUnavailableError(clean_user, "unreachable", queued_files)
                        delay = 2 ** attempt
                        logging.getLogger(__name__).info(
                            "Cannot reach Soulseek user %s; retrying in %ss (attempt %s/3)…",
                            clean_user, delay, attempt + 2,
                        )
                        time.sleep(delay)
                        continue
                raise SlskdEnqueueError(
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
