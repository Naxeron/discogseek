"""Interpret slskd transfer history without mistaking ended transfers for success."""

import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Iterator, Optional, Set, Tuple

from discogseek.core import peer_policy as _peer_policy
from discogseek.core.peer_policy import PEER_COOLDOWN, PeerPolicy, classify_failure, peer_key


RECOVERABLE_FAILURES = {"Rejected", "TimedOut", "Errored"}
TERMINAL_FAILURES = RECOVERABLE_FAILURES | {"Cancelled", "Aborted"}
# States in which a transfer waits for a slot and moves no data.
WAITING_STATES = {"Queued", "Requested", "Initializing"}


def _hours_setting(name: str, default: float) -> float:
    try:
        hours = float(os.environ.get(name) or default)
    except ValueError:
        hours = default
    return max(hours, 0.0) * 3600


# Successful transfers waited at most a few hours for a remote slot (median
# about 3 minutes); a transfer still waiting after this is unlikely to start.
STALE_QUEUE_SECONDS = _hours_setting("DISCOGSEEK_STALE_QUEUE_HOURS", 6)
# Without access to slskd's downloads directory, assume an importer has moved
# a finished file within this window.
IMPORT_WINDOW_SECONDS = _hours_setting("DISCOGSEEK_IMPORT_WINDOW_HOURS", 24)
# slskd sets nextAttemptAt for a scheduled retry. A schedule this far in the
# past was lost (for example by a restart) and no longer means a retry.
RETRY_GRACE_SECONDS = 15 * 60
# slskd 0.26 does not report when it retries a timed-out or errored attempt (no
# nextAttemptAt); it waits at most transfers.download.retry.maxDelay, assumed
# to be RETRY_DELAY_DEFAULT_SECONDS when unknown, plus this margin.
RETRY_DELAY_DEFAULT_SECONDS = 60.0
RETRY_DELAY_MARGIN_SECONDS = 30.0
# slskd's retry budget (attempts, longest delay in seconds) for DownloadHistory
# objects built without one; see slskd_retry_budget.
_RETRY_BUDGET: ContextVar[Tuple[Optional[int], Optional[float]]] = ContextVar(
    "discogseek_slskd_retry_budget", default=(None, None),
)

_TIMESTAMP = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:[.,](\d+))?\s*(Z|[+-]\d{2}:?\d{2})?$",
    re.IGNORECASE,
)
# Characters slskd cannot use in local names; it substitutes an underscore.
_UNSAFE_NAME_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')


def source_key(username: str, filename: str) -> Tuple[str, str]:
    user = re.sub(r"\s*\(.*?\)$", "", username or "").strip()
    return user, (filename or "").replace("\\", "/")


def transfer_states(file: Dict[str, Any]) -> Set[str]:
    return {part.strip() for part in (file.get("state") or "").split(",")}


def terminal_failure(file: Dict[str, Any]) -> Optional[str]:
    states = transfer_states(file)
    if "Succeeded" in states or "Completed" not in states:
        return None
    for state in ("Cancelled", "Aborted", "Rejected", "TimedOut", "Errored"):
        if state in states:
            return state
    return None


def failure_description(file: Dict[str, Any]) -> str:
    detail = terminal_failure(file) or "Failed"
    attempts = file.get("attempts")
    if attempts:
        detail += f" (retries: {attempts})"
    reason = " ".join(str(file.get("exception") or "").split())
    if reason:
        detail += f": {reason[:240]}"
    return detail


def parse_timestamp(value: Any) -> Optional[float]:
    """Return epoch seconds for slskd timestamps; values without a zone are UTC."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()
    match = _TIMESTAMP.match(str(value).strip())
    if not match:
        return None
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    tz = timezone.utc
    if zone and zone.upper() != "Z":
        digits = zone[1:].replace(":", "")
        offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
        tz = timezone(offset if zone[0] == "+" else -offset)
    try:
        moment = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second),
                          int((fraction or "0")[:6].ljust(6, "0")), tzinfo=tz)
    except ValueError:
        return None
    # .NET reports an unset DateTime as year 1.
    return moment.timestamp() if moment.year > 1970 else None


def _now(now: Any = None) -> float:
    moment = parse_timestamp(now)
    return time.time() if moment is None else moment


def _waiting_since(file: Dict[str, Any]) -> Optional[float]:
    # slskd keeps requestedAt/enqueuedAt across retries; a later start or end
    # still shows recent activity.
    moments = [parse_timestamp(file.get(field)) for field in ("requestedAt", "enqueuedAt", "startedAt", "endedAt")]
    moments = [moment for moment in moments if moment is not None]
    return max(moments) if moments else None


def is_stale_queued(file: Dict[str, Any], now: Any = None) -> bool:
    """Whether a transfer has waited for a slot longer than STALE_QUEUE_SECONDS.

    Bytes left over from an earlier attempt do not count as progress: slskd
    keeps them for resuming while the retry waits in the queue again.
    """
    if terminal_failure(file):
        return False
    states = transfer_states(file)
    if not states & WAITING_STATES or states & {"InProgress", "Completed", "Succeeded"}:
        return False
    waiting_since = _waiting_since(file)
    return waiting_since is not None and _now(now) - waiting_since > STALE_QUEUE_SECONDS


def serving_peers(downloads: Iterable[Dict[str, Any]], now: Any = None) -> Set[str]:
    """Peers sending a file now or within STALE_QUEUE_SECONDS: their queues still move."""
    moment = _now(now)
    peers: Set[str] = set()
    for user in downloads:
        for directory in user.get("directories", []):
            for file in directory.get("files", []):
                states = transfer_states(file)
                ended = parse_timestamp(file.get("endedAt"))
                if "InProgress" in states or (
                        "Succeeded" in states and ended is not None and moment - ended < STALE_QUEUE_SECONDS):
                    peers.add(peer_key(user.get("username")))
    return peers


def _option_number(value: Any) -> Optional[float]:
    """A number from slskd's options (an int or digits), or None when unknown.

    A test double or malformed option means the setting is unknown.
    """
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def retry_option(options: Any, name: str) -> Optional[float]:
    """Return a numeric slskd download retry option (e.g. attempts, maxDelay in ms), or None."""
    for path in (("transfers", "download", "retry", name), ("transfers", "retry", name)):
        value = options
        try:
            for part in path:
                value = value[part]
        except Exception:
            continue
        number = _option_number(value)
        if number is not None:
            return number
    return None


def retry_budget(options: Any) -> Tuple[Optional[int], Optional[float]]:
    """Return slskd's download retry attempts and longest delay in seconds (None: unknown)."""
    attempts = retry_option(options, "attempts")
    max_delay = retry_option(options, "maxDelay")
    return (None if attempts is None else int(attempts),
            None if max_delay is None or max_delay < 0 else max_delay / 1000)


@contextmanager
def slskd_retry_budget(attempts: Optional[int], max_delay: Optional[float] = None) -> Iterator[None]:
    """Give DownloadHistory objects built in this context (and thread) slskd's retry budget.

    Callers such as the release browser read slskd's options once and pass
    the budget through the downloader this way; max_delay is in seconds.
    """
    token = _RETRY_BUDGET.set((attempts, max_delay))
    try:
        yield
    finally:
        _RETRY_BUDGET.reset(token)


def _attempt_count(file: Dict[str, Any]) -> int:
    try:
        return int(file.get("attempts") or 0)
    except (TypeError, ValueError):
        return 0


def is_final_failure(file: Dict[str, Any], retry_attempts: Optional[int] = None, now: Any = None) -> bool:
    """Whether a failed transfer has ended, rather than awaiting slskd's own retry."""
    failure = terminal_failure(file)
    if failure is None:
        return False
    if failure not in {"TimedOut", "Errored"}:
        return True
    retry_attempts = _option_number(retry_attempts)
    if retry_attempts is not None and _attempt_count(file) >= retry_attempts:
        return True
    next_attempt = file.get("nextAttemptAt")
    if not next_attempt:
        return True
    scheduled = parse_timestamp(next_attempt)
    # An unset (.NET year 1) or unreadable schedule is no pending retry.
    return scheduled is None or scheduled < _now(now) - RETRY_GRACE_SECONDS


def local_download_path(downloads_dir: str, filename: str) -> str:
    """Return where slskd saves a remote file: <downloads>/<remote folder>/<name>."""
    parts = [part for part in (filename or "").replace("\\", "/").split("/") if part]
    return os.path.join(downloads_dir, *parts[-2:])


def _local_download_exists(downloads_dir: str, filename: str, listings: Dict[str, Set[str]]) -> bool:
    if not (filename or "").replace("\\", "/").strip("/"):
        return False
    path = local_download_path(downloads_dir, filename)
    folder, name = os.path.split(path)
    candidates = {(folder, name)}
    if folder != downloads_dir:
        safe_folder = os.path.join(downloads_dir, _UNSAFE_NAME_CHARS.sub("_", os.path.basename(folder)))
    else:
        safe_folder = folder
    candidates.add((safe_folder, _UNSAFE_NAME_CHARS.sub("_", name)))
    for directory, base in candidates:
        if os.path.isfile(os.path.join(directory, base)):
            return True
        if directory not in listings:
            try:
                listings[directory] = set(os.listdir(directory))
            except OSError:
                listings[directory] = set()
        # slskd renames a file whose name is taken: "<stem>_<digits><ext>".
        stem, extension = os.path.splitext(base)
        renamed = re.compile(re.escape(stem) + r"_\d+" + re.escape(extension) + "$")
        if any(renamed.match(entry) for entry in listings[directory]):
            return True
    return False


def local_download_exists(downloads_dir: str, filename: str) -> bool:
    """Whether slskd's downloaded copy of a remote file is still awaiting import."""
    return _local_download_exists(downloads_dir, filename, {})


def resolve_downloads_dir(client) -> Optional[str]:
    """Find slskd's downloads directory when it is readable on this machine."""
    override = (os.environ.get("DISCOGSEEK_SLSKD_DOWNLOADS_DIR") or "").strip()
    if override:
        path = os.path.expanduser(override)
        return path if os.path.isdir(path) else None
    try:
        directory = client.get_options()["directories"]["downloads"]
        # Ignore placeholders such as test doubles; only a real path counts.
        if isinstance(directory, str) and directory and os.path.isdir(directory):
            return directory
    except Exception:
        pass
    return None


def _age(seconds: float) -> str:
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            count = int(seconds // size)
            return f"{count} {unit}{'s' if count != 1 else ''}"
    return "less than a minute"


def stale_description(file: Dict[str, Any], now: Any = None) -> str:
    detail = file.get("state") or "Queued"
    attempts = file.get("attempts")
    if attempts:
        detail += f" (retries: {attempts})"
    waiting_since = _waiting_since(file)
    if waiting_since is not None:
        detail += f": waiting for {_age(_now(now) - waiting_since)} without starting"
    return detail


def _internal_cancellations(policy) -> Set[Tuple[str, str]]:
    """(peer, transfer ID) of discogseek's cancel requests not yet seen as Cancelled.

    PeerPolicy consumes a marker once it observes the cancellation. A failed
    transfer stopped after it ended keeps its state in slskd, and its marker.
    """
    if not isinstance(policy, PeerPolicy):
        return set()
    with policy._connection() as conn:
        return {(str(user), str(transfer_id))
                for user, transfer_id in conn.execute("SELECT username, transfer_id FROM internal_cancellations")}


class DownloadHistory:
    """Protect healthy transfers and exclude failed sources and blocked peers.

    Matching code calls excludes only after matching a requested track. This
    scopes cancellation to that request, leaving unrelated history untouched.

    A transfer that has waited in a queue for too long is stale: it no longer
    protects its track, and it is cancelled once a replacement is queued. A
    peer that is serving other files works through its queue, so its waiting
    transfers are not stale. A succeeded transfer protects its track only
    while the file awaits import.

    With slskd's retry budget (retry_attempts, and retry_max_delay in seconds,
    or a slskd_retry_budget context), a timed-out or errored attempt that
    slskd will still retry is live, like the browser treats it. Without a
    budget every such record is a failure. A failure discogseek has already
    stopped (PeerPolicy keeps its internal-cancel marker) is not stopped again.
    """

    def __init__(self, downloads: Iterable[Dict[str, Any]], excluded_sources=None, peer_policy=None,
                 downloads_dir: Optional[str] = None, now: Any = None,
                 retry_attempts: Optional[int] = None, retry_max_delay: Optional[float] = None):
        downloads = list(downloads)
        self.now = _now(now)
        self.downloads_dir = downloads_dir
        if retry_attempts is None and retry_max_delay is None:
            retry_attempts, retry_max_delay = _RETRY_BUDGET.get()
        self.retry_attempts = _option_number(retry_attempts)
        retry_max_delay = _option_number(retry_max_delay)
        self.retry_wait = (RETRY_DELAY_DEFAULT_SECONDS if retry_max_delay is None or retry_max_delay < 0
                           else retry_max_delay) + RETRY_DELAY_MARGIN_SECONDS
        self._internal_cancels: Optional[Set[Tuple[str, str]]] = None
        self.peer_policy = peer_policy if peer_policy is not None else PeerPolicy()
        self.blocked_peers = (self.peer_policy.observe(downloads) if now is None
                              else self.peer_policy.observe(downloads, now=self.now))
        # Bulk cancellations by slskd or the system say nothing about a source.
        system_cancellations = getattr(self.peer_policy, "system_cancellations", None)
        system_cancelled = system_cancellations(downloads) if system_cancellations else set()
        self.excluded_sources = {source_key(*key) for key in (excluded_sources or ())}
        self.failed: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.stale: Dict[Tuple[str, str], Dict[str, Any]] = {}
        # Succeeded transfers whose file has left slskd's downloads directory.
        self.completed: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.protected: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._protected_sources: Set[Tuple[str, str]] = set()
        self.queued_filenames: Set[str] = set()
        listings: Dict[str, Set[str]] = {}
        serving = serving_peers(downloads, self.now)
        for user in downloads:
            for directory in user.get("directories", []):
                for file in directory.get("files", []):
                    filename = file.get("filename")
                    if not filename:
                        continue
                    key = source_key(user.get("username"), filename)
                    failure = terminal_failure(file)
                    if failure and self._awaiting_retry(key[0], file, failure):
                        failure = None
                    if failure:
                        if ((peer_key(key[0]), key[1], str(file.get("id") or "")) not in system_cancelled
                                and not self._cooled_down(file, failure)):
                            self._keep_latest(self.failed, key, file)
                    elif is_stale_queued(file, self.now) and peer_key(key[0]) not in serving:
                        self.stale[key] = file
                    elif "Succeeded" in transfer_states(file) and not self._awaiting_import(file, listings):
                        self._keep_latest(self.completed, key, file)
                    else:
                        self.protected[key] = file
                        self._protected_sources.add((peer_key(key[0]), key[1]))
                        self.queued_filenames.add(filename)
        # A live/successful copy of the same source takes precedence over an old
        # failed record, which must never cancel a currently healthy transfer.
        # A waiting copy is the transfer to cancel when replacing the source.
        stale_sources = {(peer_key(user), path) for user, path in self.stale}
        completed = {(peer_key(user), path): file for (user, path), file in self.completed.items()}
        for key in list(self.failed):
            source = (peer_key(key[0]), key[1])
            success = completed.get(source)
            if (source in self._protected_sources or source in stale_sources
                    or (success is not None and not self._newer(self.failed[key], success))):
                self.failed.pop(key, None)
        for key in list(self.stale):
            if (peer_key(key[0]), key[1]) in self._protected_sources:
                self.stale.pop(key, None)
        self.excluded_sources.update(self.failed)
        self.excluded_sources.update(self.stale)
        self.queued_base_filenames = {
            filename.replace("\\", "/").rsplit("/", 1)[-1].lower()
            for filename in self.queued_filenames
        }
        self._encountered: Set[Tuple[str, str]] = set()
        self._retired: Set[Tuple[str, str]] = set()

    @staticmethod
    def _ended(file: Dict[str, Any]) -> Optional[float]:
        return parse_timestamp(file.get("endedAt"))

    @classmethod
    def _newer(cls, file: Dict[str, Any], other: Dict[str, Any]) -> bool:
        ended, other_ended = cls._ended(file), cls._ended(other)
        return ended is not None and other_ended is not None and ended > other_ended

    @classmethod
    def _keep_latest(cls, records: Dict[Tuple[str, str], Dict[str, Any]], key, file) -> None:
        # Without timestamps, the later record in slskd's listing wins.
        if key not in records or not cls._newer(records[key], file):
            records[key] = file

    def _cooled_down(self, file: Dict[str, Any], failure: str) -> bool:
        # A transient refusal ("Too many files", "Overwhelmed") judges neither
        # the peer nor the file once its cooldown has passed.
        if classify_failure(failure, file.get("exception")) != PEER_COOLDOWN:
            return False
        ended = self._ended(file)
        return ended is not None and self.now - ended >= _peer_policy.PEER_COOLDOWN_SECONDS

    def _cancelled_internally(self, username: str, file: Dict[str, Any]) -> bool:
        """Whether discogseek already asked slskd to stop this transfer."""
        if self._internal_cancels is None:
            self._internal_cancels = _internal_cancellations(self.peer_policy)
        return (peer_key(username), str(file.get("id") or "")) in self._internal_cancels

    def _awaiting_retry(self, username: str, file: Dict[str, Any], failure: str) -> bool:
        """Whether slskd will retry a timed-out or errored attempt under its retry budget."""
        if (failure not in {"TimedOut", "Errored"} or self.retry_attempts is None
                or _attempt_count(file) >= self.retry_attempts):
            return False
        ended = self._ended(file)
        # slskd 0.26 reports no nextAttemptAt: the next attempt starts within
        # its longest retry delay after this one ended.
        pending = (not is_final_failure(file, self.retry_attempts, self.now)
                   or (ended is not None and self.now - ended <= self.retry_wait))
        return pending and not self._cancelled_internally(username, file)

    def _awaiting_import(self, file: Dict[str, Any], listings: Dict[str, Set[str]]) -> bool:
        if self.downloads_dir:
            return _local_download_exists(self.downloads_dir, file["filename"], listings)
        ended = self._ended(file)
        return ended is None or self.now - ended <= IMPORT_WINDOW_SECONDS

    def excludes(self, username: str, filename: str) -> bool:
        key = source_key(username, filename)
        if self.is_protected(username, filename):
            return False
        if key not in self.excluded_sources and not self.is_peer_blocked(username):
            return False
        self._encountered.add(key)
        return True

    def is_peer_blocked(self, username: str) -> bool:
        return peer_key(username) in self.blocked_peers

    def is_protected(self, username: str, filename: str) -> bool:
        user, path = source_key(username, filename)
        return (peer_key(user), path) in self._protected_sources

    def prepare_recovery(self, client, sources: Optional[Iterable[Tuple[str, str]]] = None) -> None:
        # A terminal state can briefly be visible between slskd retries, and a
        # stale transfer may still start later. Cancel the original before
        # switching sources, without deleting its history.
        pending = (
            self._encountered if sources is None else self._encountered.intersection(sources)
        ) - self._retired
        for key in sorted(pending):
            file = self.stale.get(key)
            if file is None:
                file = self.failed.get(key)
                # A completed failure already cancelled (e.g. by the browser)
                # keeps its state and marker; slskd has nothing left to stop.
                if (not file or terminal_failure(file) not in RECOVERABLE_FAILURES
                        or (file.get("id") and self._cancelled_internally(key[0], file))):
                    file = None
            if file is not None:
                transfer_id = file.get("id")
                if not transfer_id:
                    from discogseek.clients.slskd import SlskdAPIError
                    raise SlskdAPIError(
                        f"Cannot safely replace failed transfer from {key[0]}: slskd did not provide its ID."
                    )
                client.cancel_download(key[0], transfer_id)
            self._retired.add(key)

    def failure_summary(self, sources=None) -> str:
        details = []
        keys = self._encountered if sources is None else self._encountered.intersection(sources)
        for key in sorted(keys):
            if key in self.stale:
                detail = stale_description(self.stale[key], self.now)
            elif key in self.failed:
                detail = failure_description(self.failed[key])
            else:
                detail = self.blocked_peers.get(peer_key(key[0]), "previously failed source")
            entry = f"{key[0]}: {detail}"
            if entry not in details:
                details.append(entry)
        return "; ".join(details[:3]) + (f"; {len(details) - 3} more failures" if len(details) > 3 else "")
