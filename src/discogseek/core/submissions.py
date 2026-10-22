"""Remember which requested row each file accepted by slskd was submitted for.

A transfer's folder does not always name the release it was chosen for: a 12" may
come from the folder of the 2CD edition it was cut from. Once that transfer has
finished, its folder alone no longer shows that it fulfils the row, and the next
request would download the track again from another peer. The submission log keeps
(peer, remote path) -> row identity in Config.CACHE_DIR/submissions.db, so a transfer
discogseek submitted for a row keeps standing for that row.

The log is advisory. A missing, locked or unreadable database reads as empty and a
failed write is skipped: matching then falls back to the transfer's folder. A damaged
file is set aside (submissions.db.corrupt) by the next write, which starts a new log.
Reads open the file read-only, so only a write (never a dry run) creates it.
"""

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Set, Tuple

from discogseek.config import Config
from discogseek.core.peer_policy import peer_key
from discogseek.core.text import normalize_text
from discogseek.core.transfers import source_key


# Seconds to wait for another process's write before giving up on the log.
LOCK_TIMEOUT = 2.0
# Entries older than this are dropped on the next write; slskd keeps no transfer that long.
RETENTION_SECONDS = 365 * 24 * 3600.0
_QUERY_CHUNK = 400
_WRITE_LOCK = threading.Lock()
_logger = logging.getLogger(__name__)

Source = Tuple[str, str]


def submission_source(username: str, filename: str) -> Source:
    """The peer and remote path of a submission, as DownloadHistory identifies transfers."""
    user, path = source_key(username, filename)
    return peer_key(user), path


def row_identity(row: Dict[str, Any], release_title: str, position: Tuple[Any, Any],
                 release_artist: str) -> Optional[str]:
    """
    The requested row a file stands for: its MusicBrainz track ID, or else its recording
    ID, the release artist and the row's credit, release title, (medium, position) and
    title. None when the row cannot be told apart.
    """
    track_id = str(row.get("mb_track_id") or "").strip()
    if track_id:
        return f"track:{track_id}"
    title = normalize_text(row.get("title"))
    release = normalize_text(release_title)
    if not (title and release):
        return None
    recording = str(row.get("mb_recording_id") or "").strip()
    # Two artists' 'Greatest Hits' share a title, a position and an 'Intro'.
    credits = f"{normalize_text(release_artist)}|{normalize_text(row.get('artist'))}"
    medium, number = position
    return f"row:{recording}|{credits}|{release}|{medium}|{number}|{title}"


class SubmissionLog:
    """(peer, remote path) -> identities of the rows slskd accepted the file for."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path is not None else Path(Config.CACHE_DIR) / "submissions.db"

    def _connect(self, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            # mode=ro never creates the file, even when it vanished after the check.
            uri = self.db_path.absolute().as_uri() + "?mode=ro"
            return sqlite3.connect(uri, uri=True, timeout=LOCK_TIMEOUT)
        return sqlite3.connect(str(self.db_path), timeout=LOCK_TIMEOUT)

    def identities(self, sources: Iterable[Tuple[str, str]]) -> Dict[Source, Set[str]]:
        """Logged row identities of these (username, filename) sources; {} without a log."""
        wanted = {submission_source(user, filename) for user, filename in sources}
        if not wanted:
            return {}
        users = sorted({user for user, _ in wanted})
        found: Dict[Source, Set[str]] = {}
        try:
            if not self.db_path.is_file():
                return {}
            conn = self._connect(read_only=True)
            try:
                for start in range(0, len(users), _QUERY_CHUNK):
                    chunk = users[start:start + _QUERY_CHUNK]
                    rows = conn.execute(
                        "SELECT username, path, identity FROM submissions WHERE username IN "
                        f"({', '.join('?' * len(chunk))})",
                        chunk,
                    )
                    for user, path, identity in rows:
                        if (user, path) in wanted:
                            found.setdefault((user, path), set()).add(identity)
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as error:
            # No table yet, a lock held past the timeout, a damaged file or an inaccessible folder.
            _logger.info("Cannot read the submission log %s: %s", self.db_path, error)
            return {}
        return found

    def record(self, entries: Iterable[Tuple[str, str, Optional[str]]], now: Optional[float] = None) -> None:
        """Log (username, filename, row identity) of files slskd accepted; rows without an identity are skipped."""
        rows = sorted({(*submission_source(user, filename), identity)
                       for user, filename, identity in entries if identity and filename})
        if not rows:
            return
        moment = time.time() if now is None else float(now)
        try:
            with _WRITE_LOCK:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    self._write(rows, moment)
                except sqlite3.DatabaseError as error:
                    # A locked or busy log (OperationalError) is left alone; only a file that
                    # is no database, or a malformed one, is set aside for a new log.
                    if type(error) is not sqlite3.DatabaseError:
                        raise
                    damaged = self.db_path.with_name(self.db_path.name + ".corrupt")
                    os.replace(str(self.db_path), str(damaged))
                    _logger.warning("Set aside the damaged submission log %s as %s: %s", self.db_path, damaged, error)
                    self._write(rows, moment)
        except (sqlite3.Error, OSError) as error:
            _logger.warning("Could not record %d submissions in %s: %s", len(rows), self.db_path, error)

    def _write(self, rows, moment: float) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute("""CREATE TABLE IF NOT EXISTS submissions (
                    username TEXT NOT NULL, path TEXT NOT NULL, identity TEXT NOT NULL,
                    submitted_at REAL NOT NULL, PRIMARY KEY (username, path, identity)
                )""")
                conn.execute("CREATE INDEX IF NOT EXISTS submissions_age ON submissions (submitted_at)")
                conn.executemany(
                    "INSERT INTO submissions (username, path, identity, submitted_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(username, path, identity) DO UPDATE SET submitted_at = excluded.submitted_at",
                    [(*row, moment) for row in rows],
                )
                conn.execute("DELETE FROM submissions WHERE submitted_at < ?", (moment - RETENTION_SECONDS,))
        finally:
            conn.close()
