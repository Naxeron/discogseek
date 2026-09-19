"""Interpret slskd transfer history without mistaking ended transfers for success."""

import re
from typing import Any, Dict, Iterable, Optional, Set, Tuple


RECOVERABLE_FAILURES = {"Rejected", "TimedOut", "Errored"}
TERMINAL_FAILURES = RECOVERABLE_FAILURES | {"Cancelled", "Aborted"}


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


class DownloadHistory:
    """Protect healthy transfers and exclude exact failed sources from new searches.

    Matching code calls excludes only after matching a requested track. This
    scopes cancellation to that request, leaving unrelated history untouched.
    """

    def __init__(self, downloads: Iterable[Dict[str, Any]], excluded_sources=None):
        self.excluded_sources = {source_key(*key) for key in (excluded_sources or ())}
        self.failed: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.protected: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.queued_filenames: Set[str] = set()
        protected = set()
        for user in downloads:
            for directory in user.get("directories", []):
                for file in directory.get("files", []):
                    filename = file.get("filename")
                    if not filename:
                        continue
                    key = source_key(user.get("username"), filename)
                    if terminal_failure(file):
                        self.failed[key] = file
                    else:
                        protected.add(key)
                        self.protected[key] = file
                        self.queued_filenames.add(filename)
        # A live/successful copy of the same source takes precedence over an old
        # failed record, which must never cancel a currently healthy transfer.
        for key in protected:
            self.failed.pop(key, None)
        self.excluded_sources.update(self.failed)
        self.queued_base_filenames = {
            filename.replace("\\", "/").rsplit("/", 1)[-1].lower()
            for filename in self.queued_filenames
        }
        self._encountered: Set[Tuple[str, str]] = set()
        self._retired: Set[Tuple[str, str]] = set()

    def excludes(self, username: str, filename: str) -> bool:
        key = source_key(username, filename)
        if key not in self.excluded_sources:
            return False
        self._encountered.add(key)
        return True

    def is_protected(self, username: str, filename: str) -> bool:
        return source_key(username, filename) in self.protected

    def prepare_recovery(self, client) -> None:
        # A terminal state can briefly be visible between slskd retries. Cancel
        # the original before switching sources, without deleting its history.
        for key in sorted(self._encountered - self._retired):
            file = self.failed.get(key)
            if file and terminal_failure(file) in RECOVERABLE_FAILURES:
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
            file = self.failed.get(key)
            detail = failure_description(file) if file else "previously failed source"
            entry = f"{key[0]}: {detail}"
            if entry not in details:
                details.append(entry)
        return "; ".join(details[:3]) + (f"; {len(details) - 3} more failures" if len(details) > 3 else "")
