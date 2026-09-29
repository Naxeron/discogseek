"""Persistent peer refusals and explicit cancellations survive application sessions."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from discogseek.core.peer_policy import PeerPolicy, peer_key


def history(state="Completed, Rejected", username="Peer", **fields):
    file = {"id": "transfer", "filename": "Album\\Song.flac", "state": state, **fields}
    return [{"username": username, "directories": [{"files": [file]}]}]


@pytest.mark.parametrize("state", ["Rejected", "Aborted", "Cancelled"])
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


def test_internal_cancellation_does_not_hide_a_rejection_or_remote_abort():
    policy = PeerPolicy()
    policy.record_internal_cancel("peer", "transfer")

    assert policy.observe(history()) == {"peer": "Rejected"}
    assert policy.observe(history("Completed, Aborted")) == {"peer": "Aborted"}


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
    {"id": "new-transfer"}, {"attempts": 2}, {"state": "Completed, Aborted"},
    {"endedAt": "2026-09-29T10:00:00Z"},
])
def test_unblock_ignores_unchanged_history_but_new_events_block_again(tmp_path, new_fields):
    path = tmp_path / "policy.db"
    policy = PeerPolicy(path)
    policy.observe(history(attempts=1))

    assert policy.unblock("PEER (FLAC)")
    assert not policy.unblock("peer")
    reopened = PeerPolicy(path)
    assert reopened.observe(history(attempts=1)) == {}
    assert reopened.observe(history(**{"attempts": 1, **new_fields}))


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
