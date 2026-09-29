"""Persistent exclusions for peers that refuse transfers or are manually stopped."""

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional

from discogseek.config import Config


def peer_key(username: str) -> str:
    """Use the same peer identity for decorated search names and transfer history."""
    return re.sub(r"\s*\(.*?\)$", "", username or "").strip().casefold()


class PeerPolicy:
    """Remember blocks and cancellation provenance across processes and sessions."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path or Config.CACHE_DIR / "peer_policy.db").resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection(write=True) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS blocked_peers (
                username TEXT PRIMARY KEY, reason TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS observed_failures (
                username TEXT NOT NULL, signature TEXT NOT NULL,
                PRIMARY KEY (username, signature)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS internal_cancellations (
                username TEXT NOT NULL, transfer_id TEXT NOT NULL,
                PRIMARY KEY (username, transfer_id)
            )""")

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

    @staticmethod
    def _blocks(conn: sqlite3.Connection) -> Dict[str, str]:
        return dict(conn.execute("SELECT username, reason FROM blocked_peers ORDER BY username"))

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

    def observe(self, downloads: Iterable[Dict[str, Any]]) -> Dict[str, str]:
        """Record new refusal/manual-stop events and return all persistent blocks."""
        from discogseek.core.transfers import failure_description, terminal_failure

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

        with self._connection(write=True) as conn:
            internal = set(conn.execute("SELECT username, transfer_id FROM internal_cancellations"))
            for source, file, failure in failures:
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
                    continue
                if source in protected:
                    continue
                conn.execute(
                    "INSERT INTO blocked_peers (username, reason) VALUES (?, ?) "
                    "ON CONFLICT(username) DO UPDATE SET reason = excluded.reason",
                    (username, failure_description(file)),
                )
            return self._blocks(conn)

    def blocked_peers(self) -> Dict[str, str]:
        with self._connection() as conn:
            return self._blocks(conn)

    def blocked_reason(self, username: str) -> Optional[str]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT reason FROM blocked_peers WHERE username = ?", (peer_key(username),),
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
