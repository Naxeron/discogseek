"""Persistent peer blocks can be inspected and lifted without contacting slskd."""

from datetime import datetime

import pytest
import requests

from discogseek.cli.main import main
from discogseek.clients.slskd import SlskdClient
from discogseek.config import Config


@pytest.fixture(autouse=True)
def require_offline_commands(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("Peer management must not authenticate or access the network")

    monkeypatch.setattr(Config, "SLSKD_API_KEY", "")
    monkeypatch.setattr(Config, "SLSKD_USERNAME", "")
    monkeypatch.setattr(Config, "SLSKD_PASSWORD", "")
    monkeypatch.setattr(SlskdClient, "_ensure_authenticated", fail)
    monkeypatch.setattr(requests.Session, "request", fail)


@pytest.fixture
def blocked_peers():
    from discogseek.core.peer_policy import PeerPolicy

    downloads = [
        {"username": user, "directories": [{"files": [{
            "id": f"transfer-{user}", "filename": "Album/Track.flac",
            "state": f"Completed, {state}",
        }]}]}
        for user, state in [("peer-z", "Rejected"), ("peer-a", "Cancelled")]
    ]
    policy = PeerPolicy()
    policy.observe(downloads)
    return policy, downloads


@pytest.mark.parametrize("command", [["peers"], ["peers", "list"]])
def test_peers_lists_persisted_blocks_and_reasons_offline(blocked_peers, capsys, command):
    policy, _ = blocked_peers

    assert main(command) == 0

    output = capsys.readouterr()
    assert output.err == ""
    blocks = policy.peer_blocks()
    assert output.out.splitlines() == [
        "Blocked peers: 2",
        # A cooldown (bare rejection) shows when it ends; a manual cancellation lasts until unblocked.
        "peer-a | " + blocks["peer-a"]["reason"],
        f"peer-z | {blocks['peer-z']['reason']} | until "
        f"{datetime.fromtimestamp(blocks['peer-z']['expires_at']):%Y-%m-%d %H:%M}",
    ]


def test_peers_unblocks_persistently_without_reblocking_old_history(blocked_peers, capsys):
    from discogseek.core.peer_policy import PeerPolicy

    _, downloads = blocked_peers

    assert main(["peers", "unblock", "peer-z"]) == 0
    assert capsys.readouterr().out == "Unblocked peer: peer-z\n"
    reopened = PeerPolicy()
    assert set(reopened.blocked_peers()) == {"peer-a"}
    reopened.observe(downloads)
    assert set(PeerPolicy().blocked_peers()) == {"peer-a"}


@pytest.mark.parametrize("command", [["peers"], ["peers", "list"]])
def test_peers_reports_empty_policy_offline(capsys, command):
    assert main(command) == 0
    output = capsys.readouterr()
    assert output.out == "No blocked peers.\n"
    assert output.err == ""


def test_peers_unblock_is_idempotent(blocked_peers, capsys):
    policy, _ = blocked_peers
    before = policy.blocked_peers()

    assert main(["peers", "unblock", "unknown-peer"]) == 0

    output = capsys.readouterr()
    assert output.out == "Peer is not blocked: unknown-peer\n"
    assert output.err == ""
    assert policy.blocked_peers() == before


def test_peers_unblock_requires_username():
    with pytest.raises(SystemExit) as raised:
        main(["peers", "unblock"])
    assert raised.value.code == 2
