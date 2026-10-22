"""The submission log: which row each file slskd accepted was submitted for."""

import logging
import sqlite3
import threading

import pytest

from discogseek.config import Config
from discogseek.core import submissions
from discogseek.core.submissions import SubmissionLog, row_identity, submission_source

FILE = r"music\Modeselektor - Modeselektion, Vol. 04 (2018) [FLAC] {MTR089CD}\Disc 2\05 - FJAAK - Matte.flac"
SOURCE = ("estrogenpanther", FILE.replace("\\", "/"))


def test_log_lives_in_the_cache_directory_and_is_created_only_by_a_write():
    log = SubmissionLog()

    assert log.db_path == Config.CACHE_DIR / "submissions.db"
    assert log.identities([("estrogenpanther", FILE)]) == {}
    assert not log.db_path.exists()

    log.record([("estrogenpanther", FILE, "track:a2")])

    assert log.db_path.is_file()
    assert log.identities([("estrogenpanther", FILE)]) == {SOURCE: {"track:a2"}}


def test_sources_are_read_as_transfer_history_names_them():
    log = SubmissionLog()
    log.record([("EstrogenPanther (2)", FILE, "track:a2")])

    assert submission_source("estrogenpanther", FILE.replace("\\", "/")) == SOURCE
    assert log.identities([("estrogenpanther", FILE.replace("\\", "/"))]) == {SOURCE: {"track:a2"}}
    assert log.identities([("other", FILE), ("estrogenpanther", FILE + ".mp3")]) == {}


def test_a_file_keeps_every_row_it_was_submitted_for():
    log = SubmissionLog()
    log.record([("peer", FILE, "track:12inch"), ("peer", FILE, None), ("peer", "", "track:x")])
    log.record([("peer", FILE, "track:2cd"), ("peer", FILE, "track:12inch")])

    assert log.identities([("peer", FILE)]) == {("peer", SOURCE[1]): {"track:12inch", "track:2cd"}}


def test_entries_without_an_identity_write_nothing():
    SubmissionLog().record([("peer", FILE, None), ("peer", FILE, "")])

    assert not (Config.CACHE_DIR / "submissions.db").exists()


def test_old_entries_are_dropped_on_the_next_write():
    log = SubmissionLog()
    log.record([("peer", "old.flac", "track:old")], now=1_000.0)
    log.record([("peer", "new.flac", "track:new")], now=1_000.0 + submissions.RETENTION_SECONDS + 1)

    assert log.identities([("peer", "old.flac"), ("peer", "new.flac")]) == {("peer", "new.flac"): {"track:new"}}


def test_many_threads_record_without_losing_entries():
    log = SubmissionLog()
    errors = []

    def submit(worker):
        try:
            for index in range(25):
                log.record([(f"peer{worker}", f"{index}.flac", f"track:{worker}-{index}")])
        except Exception as error:  # pragma: no cover - reported below
            errors.append(error)

    threads = [threading.Thread(target=submit, args=(worker,)) for worker in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    found = log.identities([(f"peer{worker}", f"{index}.flac") for worker in range(8) for index in range(25)])
    assert len(found) == 200


def test_a_locked_log_is_skipped_without_failing(monkeypatch, caplog):
    log = SubmissionLog()
    log.record([("peer", FILE, "track:a2")])
    monkeypatch.setattr(submissions, "LOCK_TIMEOUT", 0.05)
    holder = sqlite3.connect(str(log.db_path))
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with caplog.at_level(logging.INFO, logger="discogseek.core.submissions"):
            log.record([("peer", "other.flac", "track:b1")])
            assert log.identities([("peer", FILE)]) == {}
    finally:
        holder.rollback()
        holder.close()

    assert "Could not record 1 submissions" in caplog.text
    assert log.identities([("peer", FILE), ("peer", "other.flac")]) == {("peer", SOURCE[1]): {"track:a2"}}


@pytest.mark.parametrize("content", [b"not a database" * 100, b""])
def test_an_unreadable_log_reads_as_empty(content):
    log = SubmissionLog()
    log.db_path.parent.mkdir(parents=True, exist_ok=True)
    log.db_path.write_bytes(content)

    assert log.identities([("peer", FILE)]) == {}
    log.record([("peer", FILE, "track:a2")])  # never raises


def test_row_identity_prefers_the_musicbrainz_track():
    row = {"title": "Matte", "artist": "FJAAK", "mb_track_id": "7144eaec", "mb_recording_id": "rec-1"}
    release = "Modeselektion Vol.04 #2"

    assert row_identity(row, release, (1, "A2"), "Various Artists") == "track:7144eaec"
    assert row_identity(dict(row, mb_track_id=None), release, (1, "A2"), "Various Artists") == (
        "row:rec-1|various artists|fjaak|modeselektion vol 04 2|1|A2|matte"
    )
    assert row_identity({"title": "Matte"}, release, (2, 5), "Various Artists") == (
        "row:|various artists||modeselektion vol 04 2|2|5|matte"
    )
    assert row_identity({"title": ""}, "Album", (1, 1), "Artist") is None
    assert row_identity({"title": "Matte"}, "", (1, 1), "Artist") is None


def test_row_identity_tells_apart_releases_of_the_same_title_by_other_artists():
    """Two artists' 'Greatest Hits' share a title, a position and an 'Intro'."""
    intro = {"title": "Intro"}

    assert row_identity(intro, "Greatest Hits", (1, 1), "Alpha") != row_identity(intro, "Greatest Hits", (1, 1), "Beta")
    assert row_identity(dict(intro, artist="Alpha"), "Hits", (1, 1), "Various Artists") != row_identity(
        dict(intro, artist="Beta"), "Hits", (1, 1), "Various Artists")


def test_reading_never_creates_the_log(monkeypatch):
    """Not even when the file disappears between the check and the read (another process, a cleanup)."""
    log = SubmissionLog()
    log.db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(submissions.Path, "is_file", lambda self: True)

    assert log.identities([("peer", FILE)]) == {}
    assert not log.db_path.exists()


def test_an_inaccessible_cache_directory_reads_as_empty(monkeypatch):
    """Before Python 3.14 Path.is_file raises PermissionError for a directory without search permission."""
    def denied(self):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(submissions.Path, "is_file", denied)

    assert SubmissionLog().identities([("peer", FILE)]) == {}


def test_a_damaged_log_is_set_aside_and_started_again(caplog):
    log = SubmissionLog()
    log.db_path.parent.mkdir(parents=True, exist_ok=True)
    log.db_path.write_bytes(b"not a database" * 100)

    with caplog.at_level(logging.WARNING, logger="discogseek.core.submissions"):
        log.record([("peer", FILE, "track:a2")])

    assert log.identities([("peer", FILE)]) == {("peer", SOURCE[1]): {"track:a2"}}
    assert log.db_path.with_name("submissions.db.corrupt").read_bytes() == b"not a database" * 100
    assert "Set aside the damaged submission log" in caplog.text
