"""Persistent exclusions for peers that refuse transfers or are manually stopped."""

import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple

from discogseek.config import Config


# Failure classes. Only PEER_BLOCK is a lasting judgment of the whole peer.
PEER_BLOCK = "block"
PEER_COOLDOWN = "cooldown"
FILE_ONLY = "file"

PERMANENT_REASONS = re.compile(r"\bbanned\b|verification required|blocked country", re.IGNORECASE)
# The peer no longer offers this one file (stale share index, read failure, or
# a re-tagged file whose size changed). DownloadHistory excludes that file.
FILE_REASONS = re.compile(
    r"not shared|file read error|remote file error|disallowed extension"
    r"|does not match expected size|size mismatch",
    re.IGNORECASE,
)
# At least this many cancellations ending within BULK_CANCEL_WINDOW seconds of
# each other come from slskd or a bulk cleanup, not a judgment of one peer.
BULK_CANCEL_MIN = 3
BULK_CANCEL_WINDOW = 1.0


def _cooldown_seconds() -> float:
    try:
        hours = float(os.environ.get("DISCOGSEEK_PEER_COOLDOWN_HOURS") or 6)
    except ValueError:
        hours = 6.0
    return max(hours, 0.0) * 3600


PEER_COOLDOWN_SECONDS = _cooldown_seconds()

_STORED_REASON = re.compile(
    r"^\s*(Rejected|Aborted|Cancelled|TimedOut|Errored|Failed)\b(?:\s*\(retries:\s*\d+\))?\s*(?::\s*(.*))?$",
    re.DOTALL,
)


def peer_key(username: str) -> str:
    """Use the same peer identity for decorated search names and transfer history."""
    return re.sub(r"\s*\(.*?\)$", "", username or "").strip().casefold()


def classify_failure(failure: Optional[str], reason: Any = "") -> Optional[str]:
    """Classify a terminal failure state and slskd's exception text.

    Returns PEER_BLOCK, PEER_COOLDOWN, FILE_ONLY, or None for failures that
    say nothing about the peer (timeouts and connection errors). A Cancelled
    event is a manual stop here; callers rule out internal and bulk cancels.
    """
    text = " ".join(str(reason or "").split())
    if failure == "Cancelled":
        return PEER_BLOCK
    if failure == "Aborted":
        # slskd aborts a transfer only when the remote size differs.
        return FILE_ONLY
    if failure != "Rejected":
        return None
    if PERMANENT_REASONS.search(text):
        return PEER_BLOCK
    if FILE_REASONS.search(text):
        return FILE_ONLY
    # Queue limits ("Too many files"), "Overwhelmed … try again later", and
    # "Enqueue failed due to internal error" pass; so may an unexplained one.
    return PEER_COOLDOWN


def classify_reason(reason: str) -> Optional[str]:
    """Classify a stored block reason written by failure_description."""
    match = _STORED_REASON.match(reason or "")
    if not match:
        # An unknown format may be a deliberate block: keep it.
        return PEER_BLOCK
    return classify_failure(match.group(1), match.group(2) or "")


def _bulk_cancellations(files: List[Dict[str, Any]]) -> Set[int]:
    """Return indexes of cancelled transfers that ended together with others."""
    from discogseek.core.transfers import parse_timestamp

    moments = [(parse_timestamp(file.get("endedAt")), index) for index, file in enumerate(files)]
    ended = sorted((moment, index) for moment, index in moments if moment is not None)
    bulk: Set[int] = set()
    cluster: List[int] = []
    previous = None
    for moment, index in ended + [(None, None)]:
        if moment is not None and previous is not None and moment - previous <= BULK_CANCEL_WINDOW:
            cluster.append(index)
        else:
            if len(cluster) >= BULK_CANCEL_MIN:
                bulk.update(cluster)
            cluster = [index]
        previous = moment
    return bulk


class PeerPolicy:
    """Remember blocks and cancellation provenance across processes and sessions.

    Banned or verification-required refusals and genuine manual cancels block
    a peer until it is unblocked. Transient refusals start a cooldown that
    expires after PEER_COOLDOWN_SECONDS. File-level failures and bulk
    cancellations never block a peer.
    """

    def __init__(self, db_path: Optional[Path] = None, clock: Optional[Callable[[], float]] = None):
        self.db_path = Path(db_path or Config.CACHE_DIR / "peer_policy.db").resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or time.time
        with self._connection(write=True) as conn:
            # expires_at NULL means the block lasts until an explicit unblock.
            conn.execute("""CREATE TABLE IF NOT EXISTS blocked_peers (
                username TEXT PRIMARY KEY, reason TEXT NOT NULL,
                blocked_at REAL, expires_at REAL, transfer_id TEXT
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS observed_failures (
                username TEXT NOT NULL, signature TEXT NOT NULL, outcome TEXT,
                PRIMARY KEY (username, signature)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS internal_cancellations (
                username TEXT NOT NULL, transfer_id TEXT NOT NULL,
                PRIMARY KEY (username, transfer_id)
            )""")
            self._add_columns(conn, "blocked_peers", {
                "blocked_at": "REAL", "expires_at": "REAL", "transfer_id": "TEXT",
            })
            self._add_columns(conn, "observed_failures", {"outcome": "TEXT"})
            self._reclassify(conn, self._now())

    @contextmanager
    def _connection(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        try:
            with conn:
                if write:
                    # Serialize observation and unblock so each event is handled
                    # once, including when separate CLI/browser processes run.
                    conn.execute("BEGIN IMMEDIATE")
                yield conn
        finally:
            conn.close()

    def _now(self, now: Any = None) -> float:
        from discogseek.core.transfers import parse_timestamp

        moment = parse_timestamp(now)
        return self._clock() if moment is None else moment

    @staticmethod
    def _add_columns(conn: sqlite3.Connection, table: str, columns: Dict[str, str]) -> None:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, kind in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

    @staticmethod
    def _reclassify(conn: sqlite3.Connection, now: float) -> None:
        # Apply the current policy to stored blocks, including those written
        # before failures were classified, which blocked any refusal forever.
        rows = conn.execute("SELECT username, reason, blocked_at, expires_at FROM blocked_peers").fetchall()
        for username, reason, blocked_at, expires_at in rows:
            kind = classify_reason(reason)
            if kind == PEER_BLOCK:
                if expires_at is not None:
                    conn.execute("UPDATE blocked_peers SET expires_at = NULL WHERE username = ?", (username,))
            elif kind == PEER_COOLDOWN and blocked_at is not None and blocked_at + PEER_COOLDOWN_SECONDS > now:
                if expires_at != blocked_at + PEER_COOLDOWN_SECONDS:
                    conn.execute(
                        "UPDATE blocked_peers SET expires_at = ? WHERE username = ?",
                        (blocked_at + PEER_COOLDOWN_SECONDS, username),
                    )
            else:
                # File-level reasons never block; a cooldown without a known
                # start has expired.
                conn.execute("DELETE FROM blocked_peers WHERE username = ?", (username,))

    @staticmethod
    def _blocks(conn: sqlite3.Connection, now: float) -> Dict[str, str]:
        return dict(conn.execute(
            "SELECT username, reason FROM blocked_peers "
            "WHERE expires_at IS NULL OR expires_at > ? ORDER BY username",
            (now,),
        ))

    @staticmethod
    def _signature(file: Dict[str, Any], failure: str) -> str:
        # Do not include volatile polling fields or nextAttemptAt. A new transfer
        # ID, failure, or attempt is a new event after an explicit unblock.
        event = {
            "id": str(file.get("id") or ""),
            "filename": (file.get("filename") or "").replace("\\", "/"),
            "failure": failure,
            "attempts": file.get("attempts"),
            "exception": file.get("exception"),
            "enqueuedAt": file.get("enqueuedAt"),
            "endedAt": file.get("endedAt"),
        }
        payload = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _block(conn: sqlite3.Connection, username: str, file: Dict[str, Any], reason: str,
               blocked_at: float, expires_at: Optional[float]) -> None:
        transfer_id = str(file.get("id") or "") or None
        if expires_at is None:
            # A cancel may later prove to be part of a bulk event and be lifted,
            # so it must not replace a refusal block such as Banned.
            conn.execute(
                "INSERT INTO blocked_peers (username, reason, blocked_at, expires_at, transfer_id) "
                "VALUES (?, ?, ?, NULL, ?) ON CONFLICT(username) DO UPDATE SET reason = excluded.reason, "
                "blocked_at = excluded.blocked_at, expires_at = NULL, transfer_id = excluded.transfer_id "
                "WHERE blocked_peers.expires_at IS NOT NULL OR blocked_peers.reason LIKE 'Cancelled%' "
                "OR excluded.reason NOT LIKE 'Cancelled%'",
                (username, reason, blocked_at, transfer_id),
            )
            return
        # A cooldown only extends a shorter cooldown; a lasting block stays.
        conn.execute(
            "INSERT INTO blocked_peers (username, reason, blocked_at, expires_at, transfer_id) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(username) DO UPDATE SET reason = excluded.reason, "
            "blocked_at = excluded.blocked_at, expires_at = excluded.expires_at, "
            "transfer_id = excluded.transfer_id "
            "WHERE blocked_peers.expires_at IS NOT NULL AND blocked_peers.expires_at < excluded.expires_at",
            (username, reason, blocked_at, expires_at, transfer_id),
        )

    @staticmethod
    def _collect(downloads: Iterable[Dict[str, Any]]):
        from discogseek.core.transfers import terminal_failure

        protected = set()
        failures = []
        for user in downloads:
            username = peer_key(user.get("username"))
            if not username:
                continue
            for directory in user.get("directories", []):
                for file in directory.get("files", []):
                    filename = file.get("filename")
                    if not filename:
                        continue
                    source = (username, filename.replace("\\", "/"))
                    failure = terminal_failure(file)
                    if failure is None:
                        protected.add(source)
                    elif failure in {"Rejected", "Aborted", "Cancelled"}:
                        failures.append((source, file, failure))
        cancelled = [index for index, (_, _, failure) in enumerate(failures) if failure == "Cancelled"]
        bulk = {cancelled[index] for index in _bulk_cancellations([failures[i][1] for i in cancelled])}
        return protected, failures, bulk

    def observe(self, downloads: Iterable[Dict[str, Any]], now: Any = None) -> Dict[str, str]:
        """Record new refusal/manual-stop events and return all active blocks."""
        from discogseek.core.transfers import failure_description, parse_timestamp

        now = self._now(now)
        protected, failures, bulk = self._collect(downloads)

        with self._connection(write=True) as conn:
            conn.execute("DELETE FROM blocked_peers WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,))
            internal = set(conn.execute("SELECT username, transfer_id FROM internal_cancellations"))
            for index, (source, file, failure) in enumerate(failures):
                username = source[0]
                signature = self._signature(file, failure)
                inserted = conn.execute(
                    "INSERT OR IGNORE INTO observed_failures (username, signature) VALUES (?, ?)",
                    (username, signature),
                ).rowcount
                # Remember overridden and internal events too: a stale record
                # must not become a fresh block after its healthy copy is cleared.
                if not inserted:
                    continue
                cancellation = (username, str(file.get("id") or ""))
                if failure == "Cancelled" and cancellation in internal:
                    # A marker explains one cancellation event, not every future
                    # attempt of a transfer that slskd resumes under the same ID.
                    # The saved signature keeps repeated snapshots exempt after
                    # consuming the marker, including overridden healthy copies.
                    conn.execute(
                        "DELETE FROM internal_cancellations WHERE username = ? AND transfer_id = ?",
                        cancellation,
                    )
                    internal.remove(cancellation)
                    outcome = "internal"
                elif source in protected:
                    outcome = "protected"
                elif index in bulk:
                    outcome = "bulk"
                else:
                    outcome = classify_failure(failure, file.get("exception")) or "ignored"
                    ended = parse_timestamp(file.get("endedAt"))
                    if outcome == PEER_BLOCK:
                        self._block(conn, username, file, failure_description(file),
                                    now if ended is None else ended, None)
                    elif outcome == PEER_COOLDOWN:
                        # Count the cooldown from the refusal, so an old event
                        # seen for the first time does not block anything.
                        started = now if ended is None else min(ended, now)
                        if started + PEER_COOLDOWN_SECONDS > now:
                            self._block(conn, username, file, failure_description(file),
                                        started, started + PEER_COOLDOWN_SECONDS)
                conn.execute(
                    "UPDATE observed_failures SET outcome = ? WHERE username = ? AND signature = ?",
                    (outcome, username, signature),
                )
            # A poll during a bulk cancellation can see only its first events.
            # Lift a manual-cancel block once its event proves to be part of one.
            bulk_events: Dict[str, List[Dict[str, Any]]] = {}
            for index in bulk:
                bulk_events.setdefault(failures[index][0][0], []).append(failures[index][1])
            for username, files in bulk_events.items():
                row = conn.execute(
                    "SELECT reason, transfer_id FROM blocked_peers WHERE username = ? AND expires_at IS NULL",
                    (username,),
                ).fetchone()
                if not row or not row[0].startswith("Cancelled"):
                    continue
                reason, transfer_id = row
                # Blocks stored before transfer IDs were kept match by reason.
                if any(transfer_id == str(file.get("id") or "") if transfer_id
                       else reason == failure_description(file) for file in files):
                    conn.execute("DELETE FROM blocked_peers WHERE username = ?", (username,))
            return self._blocks(conn, now)

    def system_cancellations(self, downloads: Iterable[Dict[str, Any]]) -> Set[Tuple[str, str, str]]:
        """Return (peer, path, transfer ID) of bulk cancellations not made by discogseek.

        Such a cancellation says nothing about its source, unlike discogseek's
        own cancellation of a failed or stale transfer.
        """
        _, failures, bulk = self._collect(downloads)
        if not bulk:
            return set()
        with self._connection() as conn:
            internal_events = set(conn.execute(
                "SELECT username, signature FROM observed_failures WHERE outcome = 'internal'"
            ))
            markers = set(conn.execute("SELECT username, transfer_id FROM internal_cancellations"))
        cancellations = set()
        for index in bulk:
            (username, path), file, failure = failures[index]
            transfer_id = str(file.get("id") or "")
            if ((username, transfer_id) in markers
                    or (username, self._signature(file, failure)) in internal_events):
                continue
            cancellations.add((username, path, transfer_id))
        return cancellations

    def blocked_peers(self, now: Any = None) -> Dict[str, str]:
        with self._connection() as conn:
            return self._blocks(conn, self._now(now))

    def peer_blocks(self, now: Any = None) -> Dict[str, Dict[str, Any]]:
        """Return active blocks with their start and expiry (None: until unblocked)."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT username, reason, blocked_at, expires_at FROM blocked_peers "
                "WHERE expires_at IS NULL OR expires_at > ? ORDER BY username",
                (self._now(now),),
            ).fetchall()
        return {
            username: {"reason": reason, "blocked_at": blocked_at, "expires_at": expires_at}
            for username, reason, blocked_at, expires_at in rows
        }

    def blocked_reason(self, username: str, now: Any = None) -> Optional[str]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT reason FROM blocked_peers WHERE username = ? AND (expires_at IS NULL OR expires_at > ?)",
                (peer_key(username), self._now(now)),
            ).fetchone()
            return row[0] if row else None

    def record_internal_cancel(self, username: str, transfer_id: str) -> None:
        if not transfer_id:
            raise ValueError("An internal cancellation requires a transfer ID")
        with self._connection(write=True) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO internal_cancellations (username, transfer_id) VALUES (?, ?)",
                (peer_key(username), str(transfer_id)),
            )

    def forget_internal_cancel(self, username: str, transfer_id: str) -> None:
        with self._connection(write=True) as conn:
            conn.execute(
                "DELETE FROM internal_cancellations WHERE username = ? AND transfer_id = ?",
                (peer_key(username), str(transfer_id)),
            )

    def unblock(self, username: str) -> bool:
        """Unblock a peer without replaying failures already observed for it."""
        with self._connection(write=True) as conn:
            return bool(conn.execute(
                "DELETE FROM blocked_peers WHERE username = ?", (peer_key(username),),
            ).rowcount)
