"""Persistent peer refusals and explicit cancellations survive application sessions."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from discogseek.core.peer_policy import (
    FILE_ONLY, PEER_BLOCK, PEER_COOLDOWN, PEER_COOLDOWN_SECONDS, PeerPolicy, classify_failure, classify_reason,
    peer_key,
)
from discogseek.core.transfers import failure_description


def history(state="Completed, Rejected", username="Peer", **fields):
    file = {"id": "transfer", "filename": "Album\\Song.flac", "state": state, **fields}
    return [{"username": username, "directories": [{"files": [file]}]}]


@pytest.mark.parametrize("state", ["Rejected", "Cancelled"])
def test_peer_block_survives_new_policy_and_cleared_slskd_history(tmp_path, state):
    path = tmp_path / "policy.db"
    policy = PeerPolicy(path)

    assert policy.observe(history(f"Completed, {state}", attempts=4, exception="Not available")) == {
        "peer": f"{state} (retries: 4): Not available",
    }
    reopened = PeerPolicy(path)
    assert reopened.observe([]) == policy.blocked_peers()
    assert reopened.blocked_reason("PEER (FLAC)") == f"{state} (retries: 4): Not available"


@pytest.mark.parametrize("state", [
    "Completed, TimedOut", "Completed, Errored", "Queued, Remotely", "InProgress",
    "Completed, Rejected, Succeeded", "Rejected", "Cancelled", "Completed, Unknown", "",
])
def test_network_failures_and_healthy_or_unknown_transfers_do_not_block(state):
    policy = PeerPolicy()

    assert policy.observe(history(state, attempts=100, exception="Old failure")) == {}


def test_internal_cancellation_provenance_survives_sessions(tmp_path):
    path = tmp_path / "policy.db"
    policy = PeerPolicy(path)
    policy.record_internal_cancel("PEER (FLAC)", "transfer")

    reopened = PeerPolicy(path)
    assert reopened.observe(history("Completed, Cancelled, Errored")) == {}
    assert reopened.observe(history("Completed, Cancelled", id="user-stopped-transfer")) == {
        "peer": "Cancelled",
    }


@pytest.mark.parametrize("new_fields", [
    {"attempts": 2}, {"endedAt": "2026-09-29T11:00:00Z"},
])
@pytest.mark.parametrize("healthy_copy", [False, True])
def test_internal_cancel_marker_does_not_hide_later_manual_cancel(tmp_path, new_fields, healthy_copy):
    path = tmp_path / "policy.db"
    policy = PeerPolicy(path)
    policy.record_internal_cancel("peer", "transfer")
    first_fields = {"attempts": 1, "endedAt": "2026-09-29T10:00:00Z"}
    internal_cancel = history("Completed, Cancelled", **first_fields)
    snapshot = internal_cancel
    if healthy_copy:
        snapshot += history("InProgress", id="healthy-copy")

    assert policy.observe(snapshot) == {}
    reopened = PeerPolicy(path)
    assert reopened.observe(history("Completed, Cancelled", **first_fields)) == {}
    assert reopened.observe(history("InProgress", attempts=2)) == {}

    manual_fields = {**first_fields, **new_fields}
    assert reopened.observe(history("Completed, Cancelled", **manual_fields)) == {
        "peer": f"Cancelled (retries: {manual_fields['attempts']})",
    }


def test_repeated_old_cancel_snapshot_does_not_consume_a_new_internal_marker():
    policy = PeerPolicy()
    policy.record_internal_cancel("peer", "transfer")
    first_cancel = history("Completed, Cancelled", attempts=1)
    assert policy.observe(first_cancel) == {}

    policy.record_internal_cancel("peer", "transfer")
    assert policy.observe(first_cancel) == {}
    assert policy.observe(history("Completed, Cancelled", attempts=2)) == {}
    assert policy.observe(history("Completed, Cancelled", attempts=3)) == {
        "peer": "Cancelled (retries: 3)",
    }


def test_internal_cancellation_does_not_hide_a_rejection():
    policy = PeerPolicy()
    policy.record_internal_cancel("peer", "transfer")

    assert policy.observe(history()) == {"peer": "Rejected"}
    assert policy.observe(history("Completed, Rejected", exception="Banned")) == {"peer": "Rejected: Banned"}


def test_failed_internal_cancellation_marker_can_be_removed():
    policy = PeerPolicy()
    policy.record_internal_cancel("PEER", "transfer")
    policy.forget_internal_cancel("peer (FLAC)", "transfer")

    assert policy.observe(history("Completed, Cancelled")) == {"peer": "Cancelled"}


def test_internal_cancellation_requires_a_transfer_id():
    with pytest.raises(ValueError, match="transfer ID"):
        PeerPolicy().record_internal_cancel("peer", "")


@pytest.mark.parametrize("healthy_state", ["Queued, Remotely", "InProgress", "Completed, Succeeded"])
@pytest.mark.parametrize("failed_first", [True, False])
def test_healthy_copy_overrides_stale_failed_record(healthy_state, failed_first):
    policy = PeerPolicy()
    failed = history()
    healthy = history(healthy_state, username="PEER (FLAC)", id="live", filename="Album/Song.flac")

    assert policy.observe(failed + healthy if failed_first else healthy + failed) == {}
    # Clearing the healthy copy later must not turn this old record into a new event.
    assert policy.observe(failed) == {}


def test_healthy_copy_does_not_erase_a_previous_persistent_block():
    policy = PeerPolicy()
    policy.observe(history())

    assert policy.observe(history("InProgress")) == {"peer": "Rejected"}


@pytest.mark.parametrize("new_fields", [
    {"id": "new-transfer"}, {"attempts": 2}, {"state": "Completed, Cancelled"},
    {"endedAt": "2026-09-29T10:00:00Z"},
])
def test_unblock_ignores_unchanged_history_but_new_events_block_again(tmp_path, new_fields):
    path = tmp_path / "policy.db"
    policy = PeerPolicy(path)
    policy.observe(history(attempts=1, exception="Banned"))

    assert policy.unblock("PEER (FLAC)")
    assert not policy.unblock("peer")
    reopened = PeerPolicy(path)
    assert reopened.observe(history(attempts=1, exception="Banned")) == {}
    assert reopened.observe(history(**{"attempts": 1, "exception": "Banned", **new_fields}))


def test_volatile_retry_schedule_does_not_undo_an_unblock():
    policy = PeerPolicy()
    policy.observe(history(nextAttemptAt="2026-09-29T10:00:00Z"))
    policy.unblock("peer")

    assert policy.observe(history(nextAttemptAt="2026-09-29T11:00:00Z")) == {}


def test_canonical_username_and_database_values_are_not_sql():
    username = "Peer'; DROP TABLE blocked_peers;-- & 音"
    policy = PeerPolicy()

    assert peer_key(f" {username} (FLAC)") == username.casefold()
    assert policy.observe(history(username=username)) == {username.casefold(): "Rejected"}
    assert policy.unblock(username.upper())
    assert policy.blocked_peers() == {}


def test_concurrent_writers_preserve_all_peer_blocks(tmp_path):
    path = tmp_path / "policy.db"

    def block(number):
        return PeerPolicy(path).observe(history(username=f"peer-{number}"))

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(block, range(20)))

    assert PeerPolicy(path).blocked_peers() == {f"peer-{number}": "Rejected" for number in range(20)}


def test_database_errors_propagate_instead_of_ignoring_blocks(tmp_path):
    path = tmp_path / "policy.db"
    policy = PeerPolicy(path)
    with sqlite3.connect(str(path)) as conn:
        conn.execute("DROP TABLE blocked_peers")

    with pytest.raises(sqlite3.OperationalError):
        policy.blocked_reason("peer")
    with pytest.raises(sqlite3.OperationalError):
        policy.observe(history())


def test_connections_close_after_success_and_error(monkeypatch):
    connect = sqlite3.connect
    connections = []

    def tracked_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr("discogseek.core.peer_policy.sqlite3.connect", tracked_connect)
    policy = PeerPolicy()
    policy.observe(history())
    policy.blocked_reason("peer")
    policy.unblock("peer")
    connection = connect(str(policy.db_path))
    try:
        connection.execute("DROP TABLE blocked_peers")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(sqlite3.OperationalError):
        policy.observe(history())

    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


HOUR = 3600
EVENT = "2026-10-09T15:30:10.6202022Z"
EVENT_TIME = 1791559810.6202022  # EVENT as epoch seconds


@pytest.mark.parametrize("failure,exception,expected", [
    ("Rejected", "Transfer rejected: Banned", PEER_BLOCK),
    ("Rejected", "Verification required", PEER_BLOCK),
    ("Cancelled", "A task was canceled.", PEER_BLOCK),
    ("Rejected", "Too many files", PEER_COOLDOWN),
    ("Rejected", "Transfer rejected: Too many files", PEER_COOLDOWN),
    ("Rejected", "Transfer rejected: Overwhelmed with requests; try again later.", PEER_COOLDOWN),
    ("Rejected", "Transfer rejected: Enqueue failed due to internal error", PEER_COOLDOWN),
    ("Rejected", "", PEER_COOLDOWN),
    ("Rejected", "Transfer rejected: File not shared.", FILE_ONLY),
    ("Rejected", "File read error.", FILE_ONLY),
    ("Aborted", "Transfer aborted: the remote size of 9299071 does not match expected size 9292132", FILE_ONLY),
    ("Aborted", "", FILE_ONLY),
    ("TimedOut", "Inactivity timeout of 60000 milliseconds was reached", None),
    ("Errored", "User godfather appears to be offline", None),
])
def test_failures_are_classified_by_reason(failure, exception, expected):
    assert classify_failure(failure, exception) == expected
    assert classify_reason(failure_description({"state": f"Completed, {failure}", "attempts": 2,
                                                "exception": exception})) == expected


def test_event_time_constant_matches_slskd_timestamp():
    from discogseek.core.transfers import parse_timestamp

    assert parse_timestamp(EVENT) == pytest.approx(EVENT_TIME)


@pytest.mark.parametrize("exception", ["Transfer rejected: Banned", "Verification required"])
def test_banned_or_verification_rejection_blocks_permanently(exception):
    policy = PeerPolicy()
    events = history(exception=exception, endedAt=EVENT)

    assert policy.observe(events, now=EVENT_TIME + 60) == {"peer": f"Rejected: {exception}"}
    assert policy.blocked_peers(now=EVENT_TIME + 365 * 24 * HOUR) == {"peer": f"Rejected: {exception}"}
    assert policy.peer_blocks(now=EVENT_TIME + 60)["peer"]["expires_at"] is None


@pytest.mark.parametrize("exception", [
    "Too many files", "Transfer rejected: Overwhelmed with requests; try again later.",
    "Transfer rejected: Enqueue failed due to internal error",
])
def test_transient_rejection_is_a_cooldown_that_expires(exception):
    policy = PeerPolicy()
    events = history(exception=exception, endedAt=EVENT, username="pavlosh")

    assert policy.observe(events, now=EVENT_TIME + 60) == {"pavlosh": f"Rejected: {exception}"}
    assert policy.blocked_reason("pavlosh", now=EVENT_TIME + 5 * HOUR)
    assert policy.peer_blocks(now=EVENT_TIME + 60)["pavlosh"]["expires_at"] == pytest.approx(
        EVENT_TIME + PEER_COOLDOWN_SECONDS)
    assert policy.observe(events, now=EVENT_TIME + 7 * HOUR) == {}
    assert policy.blocked_reason("pavlosh", now=EVENT_TIME + 7 * HOUR) is None


@pytest.mark.parametrize("state,exception", [
    ("Completed, Rejected", "Transfer rejected: File not shared."),
    ("Completed, Rejected", "File read error."),
    ("Completed, Aborted", "Transfer aborted: the remote size of 9299071 does not match expected size 9292132"),
])
def test_file_level_failure_never_blocks_the_peer(state, exception):
    policy = PeerPolicy()

    assert policy.observe(history(state, exception=exception, endedAt=EVENT), now=EVENT_TIME + 60) == {}
    assert policy.blocked_peers() == {}


def test_old_transient_refusal_seen_for_the_first_time_does_not_block():
    policy = PeerPolicy()

    assert policy.observe(history(exception="Too many files", endedAt=EVENT), now=EVENT_TIME + 12 * 24 * HOUR) == {}


def test_cooldown_survives_reopening_until_it_expires(tmp_path):
    path = tmp_path / "policy.db"
    clock = {"now": EVENT_TIME + 60}
    PeerPolicy(path, clock=lambda: clock["now"]).observe(history(exception="Too many files", endedAt=EVENT))

    clock["now"] = EVENT_TIME + 5 * HOUR
    assert PeerPolicy(path, clock=lambda: clock["now"]).blocked_peers() == {"peer": "Rejected: Too many files"}
    clock["now"] = EVENT_TIME + 7 * HOUR
    assert PeerPolicy(path, clock=lambda: clock["now"]).blocked_peers() == {}
    with sqlite3.connect(str(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM blocked_peers").fetchone() == (0,)


def test_cooldown_hours_read_environment(monkeypatch):
    from discogseek.core import peer_policy

    monkeypatch.setenv("DISCOGSEEK_PEER_COOLDOWN_HOURS", "1.5")
    assert peer_policy._cooldown_seconds() == 1.5 * HOUR
    monkeypatch.setenv("DISCOGSEEK_PEER_COOLDOWN_HOURS", "later")
    assert peer_policy._cooldown_seconds() == 6 * HOUR
    monkeypatch.setattr(peer_policy, "PEER_COOLDOWN_SECONDS", HOUR)
    policy = PeerPolicy()
    policy.observe(history(exception="Too many files", endedAt=EVENT), now=EVENT_TIME)
    assert policy.blocked_peers(now=EVENT_TIME + 2 * HOUR) == {}


def test_cooldown_never_shortens_a_permanent_block():
    policy = PeerPolicy()
    policy.observe(history(exception="Transfer rejected: Banned", endedAt=EVENT), now=EVENT_TIME)
    policy.observe(history(id="later", exception="Too many files", endedAt=EVENT), now=EVENT_TIME)

    assert policy.blocked_peers(now=EVENT_TIME + 7 * HOUR) == {"peer": "Rejected: Transfer rejected: Banned"}


def test_cancel_lifted_as_part_of_a_bulk_event_keeps_an_earlier_ban():
    policy = PeerPolicy()
    banned = history(exception="Transfer rejected: Banned", endedAt=EVENT)[0]["directories"][0]["files"]
    snapshot = bulk_cancellation({"peer": 3})
    cancels = snapshot[0]["directories"][0]["files"]
    policy.observe([{"username": "peer", "directories": [{"files": banned}]}], now=EVENT_TIME)

    # A poll sees the first cancel of a bulk event, then the whole event.
    policy.observe([{"username": "peer", "directories": [{"files": banned + cancels[:1]}]}])
    assert policy.observe([{"username": "peer", "directories": [{"files": banned + cancels}]}]) == {
        "peer": "Rejected: Transfer rejected: Banned"}


def test_permanent_block_replaces_a_cooldown_and_cooldowns_extend():
    policy = PeerPolicy()
    policy.observe(history(exception="Too many files", endedAt=EVENT), now=EVENT_TIME)
    later = "2026-10-09T18:30:10.6202022Z"
    policy.observe(history(id="later", exception="Too many files", endedAt=later), now=EVENT_TIME + 3 * HOUR)

    # An earlier refusal seen afterwards does not shorten the cooldown.
    policy.observe(history(id="earlier", exception="Overwhelmed", endedAt=EVENT), now=EVENT_TIME + 3 * HOUR)

    assert policy.blocked_peers(now=EVENT_TIME + 8 * HOUR) == {"peer": "Rejected: Too many files"}
    assert policy.blocked_peers(now=EVENT_TIME + 10 * HOUR) == {}
    policy.observe(history(id="banned", exception="Banned", endedAt=later), now=EVENT_TIME + 3 * HOUR)
    assert policy.blocked_peers(now=EVENT_TIME + 10 * HOUR) == {"peer": "Rejected: Banned"}


def test_unblock_lifts_a_cooldown():
    policy = PeerPolicy()
    policy.observe(history(exception="Too many files"))

    assert policy.unblock("peer")
    assert policy.blocked_peers() == {}


# One slskd event cancelled 52 queued transfers at 9 peers on 2026-10-09.
BULK_PEERS = {
    "canline_dsk": 19, "gepa": 14, "studio308": 13, "revco": 1, "minidisc0": 1,
    "chive9051": 1, "mmmm.mmmm": 1, "all_dodos_are_dead": 1, "Chewtoy": 1,
}


def bulk_cancellation(peers=None):
    downloads = []
    number = 0
    for username, count in (peers or BULK_PEERS).items():
        files = []
        for _ in range(count):
            number += 1
            ended = f"2026-10-09T14:58:16.{4137481 + number * 4999:07d}Z"
            files.append({
                "id": f"cancel-{number}", "filename": f"Music\\{username}\\Album\\{number:02d} Track.flac",
                "state": "Completed, Cancelled", "requestedAt": "2026-10-08T13:24:03.203966",
                "enqueuedAt": "2026-10-08T13:24:05.1642881", "startedAt": ended, "endedAt": ended,
                "bytesTransferred": 0, "attempts": 1, "exception": "A task was canceled.",
            })
        downloads.append({"username": username, "directories": [{"files": files}]})
    return downloads


def test_bulk_cancellation_across_peers_blocks_no_one():
    policy = PeerPolicy()
    snapshot = bulk_cancellation()

    assert sum(len(user["directories"][0]["files"]) for user in snapshot) == 52
    assert policy.observe(snapshot) == {}
    assert len(policy.system_cancellations(snapshot)) == 52


def test_bulk_cancellation_of_three_transfers_from_one_peer_is_not_a_judgment():
    policy = PeerPolicy()

    assert policy.observe(bulk_cancellation({"gepa": 3})) == {}


def test_isolated_manual_cancel_beside_a_bulk_event_still_blocks():
    policy = PeerPolicy()
    manual = history("Completed, Cancelled", username="otd7", id="manual",
                     endedAt="2026-10-09T15:10:00Z", exception="A task was canceled.")
    pair = bulk_cancellation({"revco": 2})

    assert policy.observe(bulk_cancellation() + manual) == {"otd7": "Cancelled: A task was canceled."}
    # Two cancellations in the same second are still individual stops.
    assert set(policy.observe(pair)) == {"otd7", "revco"}


def test_block_from_a_poll_during_a_bulk_cancellation_is_lifted():
    policy = PeerPolicy()
    snapshot = bulk_cancellation({"gepa": 14, "canline_dsk": 19})
    first_event = [{"username": "gepa", "directories": [{"files": snapshot[0]["directories"][0]["files"][:1]}]}]
    manual = history("Completed, Cancelled", username="canline_dsk", id="manual", endedAt=EVENT)

    assert set(policy.observe(first_event + manual)) == {"gepa", "canline_dsk"}
    assert policy.observe(snapshot + manual) == {"canline_dsk": "Cancelled"}


def test_own_bulk_cancellations_are_not_system_cancellations():
    policy = PeerPolicy()
    snapshot = bulk_cancellation({"godfather": 3})
    for file in snapshot[0]["directories"][0]["files"]:
        policy.record_internal_cancel("godfather", file["id"])

    assert policy.system_cancellations(snapshot) == set()
    assert policy.observe(snapshot) == {}
    assert policy.system_cancellations(snapshot) == set()
    assert PeerPolicy().system_cancellations(bulk_cancellation({"gepa": 3})) == {
        ("gepa", f"Music/gepa/Album/{number:02d} Track.flac", f"cancel-{number}") for number in range(1, 4)
    }


# Every block in the user's peer_policy.db on 2026-10-21, written before reasons were classified.
LEGACY_BLOCKS = {
    "pavlosh": "Rejected (retries: 1): Too many files",
    "wintersolstice8": "Aborted (retries: 1): Transfer aborted: the remote size of 10227029 does not match expected size 10004141",
    "qazinaqi": "Rejected (retries: 2): Transfer rejected: Banned",
    "otd7": "Cancelled (retries: 1): A task was canceled.",
    "diphenhydramine": "Aborted (retries: 1): Transfer aborted: the remote size of 7386571 does not match expected size 7554602",
    "randomname3001": "Cancelled (retries: 1): A task was canceled.",
    "1d8e0d73b6cdbc82": "Rejected (retries: 1): Transfer rejected: File not shared.",
    "swift142": "Rejected (retries: 1): Transfer rejected: Overwhelmed with requests; try again later.",
    "bigass-pussy": "Rejected (retries: 1): Transfer rejected: File not shared.",
    "sleizure": "Rejected (retries: 1): File read error.",
    "hlstr": "Rejected (retries: 1): Transfer rejected: File not shared.",
    "owosenpai": "Cancelled (retries: 8): The operation was canceled.",
    "metabalvin": "Rejected (retries: 2): Transfer rejected: Enqueue failed due to internal error",
    "h4rl": "Rejected (retries: 1): Transfer rejected: File not shared.",
    "maoamzedong": "Cancelled (retries: 1): A task was canceled.",
    "g4b4g00l": "Rejected (retries: 1): File read error.",
    "deem": "Rejected (retries: 2): Transfer rejected: Banned",
    "bobbyt420": "Aborted (retries: 1): Transfer aborted: the remote size of 39899637 does not match expected size 39714733",
    "oxym103": "Aborted (retries: 1): Transfer aborted: the remote size of 9299071 does not match expected size 9292132",
    "fkid": "Rejected (retries: 1): Transfer rejected: Banned",
    "=^-.-^=": "Rejected (retries: 1): Transfer rejected: Too many files",
    "petamount_music": "Rejected (retries: 2): Transfer rejected: Enqueue failed due to internal error",
    "notbdz": "Rejected (retries: 1): Too many files",
    "seawaffle": "Aborted (retries: 1): Transfer aborted: the remote size of 26178771 does not match expected size 26003046",
    "revco": "Cancelled (retries: 1): A task was canceled.",
    "slskp4d": "Rejected (retries: 1): File read error.",
    "dogsndabs": "Rejected (retries: 1): Transfer rejected: File not shared.",
    "fletcher00": "Rejected (retries: 1): Transfer rejected: File not shared.",
    "stereo5636": "Rejected (retries: 1): Transfer rejected: File not shared.",
    "chive9051": "Cancelled (retries: 10): A task was canceled.",
    "6gzrscnggvvnck49zfnrzalscvh2": "Rejected (retries: 1): Transfer rejected: File not shared.",
    "w00dyts": "Rejected (retries: 2): Transfer rejected: Enqueue failed due to internal error",
    "gepa": "Cancelled (retries: 1): A task was canceled.",
    "canline_dsk": "Cancelled (retries: 4): A task was canceled.",
    "studio308": "Cancelled (retries: 1): A task was canceled.",
    "minidisc0": "Cancelled (retries: 1): A task was canceled.",
    "mmmm.mmmm": "Cancelled (retries: 2): A task was canceled.",
    "all_dodos_are_dead": "Cancelled (retries: 3): A task was canceled.",
    "chewtoy": "Cancelled (retries: 1): A task was canceled.",
    "nazarene1": "Rejected (retries: 1): Verification required",
}


def legacy_database(path):
    with sqlite3.connect(str(path)) as conn:
        conn.execute("CREATE TABLE blocked_peers (username TEXT PRIMARY KEY, reason TEXT NOT NULL)")
        conn.execute("""CREATE TABLE observed_failures (
            username TEXT NOT NULL, signature TEXT NOT NULL, PRIMARY KEY (username, signature))""")
        conn.execute("""CREATE TABLE internal_cancellations (
            username TEXT NOT NULL, transfer_id TEXT NOT NULL, PRIMARY KEY (username, transfer_id))""")
        conn.executemany("INSERT INTO blocked_peers VALUES (?, ?)", LEGACY_BLOCKS.items())
    return path


def test_legacy_blocks_are_reclassified_when_the_policy_opens(tmp_path):
    path = legacy_database(tmp_path / "policy.db")

    blocked = PeerPolicy(path).blocked_peers()

    # Refusals are kept only when they judge the peer; cancellations stay
    # until history shows they were part of a bulk event.
    assert set(blocked) == {"qazinaqi", "deem", "fkid", "nazarene1"} | {
        username for username, reason in LEGACY_BLOCKS.items() if reason.startswith("Cancelled")
    }
    assert blocked["nazarene1"] == "Rejected (retries: 1): Verification required"
    reopened = PeerPolicy(path)
    assert reopened.blocked_peers() == blocked
    assert reopened.blocked_peers(now=EVENT_TIME + 10 * 365 * 24 * HOUR) == blocked


def test_legacy_bulk_cancellation_blocks_are_lifted_by_matching_history(tmp_path):
    path = legacy_database(tmp_path / "policy.db")
    policy = PeerPolicy(path)
    snapshot = bulk_cancellation({"gepa": 14, "revco": 1, "Chewtoy": 1, "canline_dsk": 2, "mmmm.mmmm": 1})
    # Old blocks carry no transfer ID: the event that wrote one has its reason.
    snapshot[3]["directories"][0]["files"][1]["attempts"] = 4

    blocked = policy.observe(snapshot)

    assert {"gepa", "revco", "chewtoy", "canline_dsk"}.isdisjoint(blocked)
    # "Cancelled (retries: 2)" came from another event than this one.
    assert {"otd7", "mmmm.mmmm", "qazinaqi"} <= set(blocked)


def test_stored_transient_block_with_a_start_becomes_a_cooldown(tmp_path):
    path = tmp_path / "policy.db"
    PeerPolicy(path)
    with sqlite3.connect(str(path)) as conn:
        conn.execute(
            "INSERT INTO blocked_peers (username, reason, blocked_at) VALUES (?, ?, ?)",
            ("swift142", "Rejected (retries: 1): Transfer rejected: Overwhelmed with requests; try again later.",
             EVENT_TIME),
        )

    assert set(PeerPolicy(path, clock=lambda: EVENT_TIME + HOUR).blocked_peers()) == {"swift142"}
    assert PeerPolicy(path, clock=lambda: EVENT_TIME + 7 * HOUR).blocked_peers() == {}


def test_unrecognised_stored_reason_is_kept(tmp_path):
    path = tmp_path / "policy.db"
    PeerPolicy(path)
    with sqlite3.connect(str(path)) as conn:
        conn.execute("INSERT INTO blocked_peers (username, reason) VALUES ('peer', 'Blocked by hand')")

    assert PeerPolicy(path).blocked_peers() == {"peer": "Blocked by hand"}
