"""SlskdClient.cancel_download against slskd 0.26 (tests/helpers/slskd026.py)."""

import pytest

from discogseek.clients.slskd import SlskdAPIError, SlskdClient
from discogseek.core.peer_policy import PeerPolicy
from tests.helpers.slskd026 import FakeSlskd026, iso

SONG = "Music\\Lone - Galaxy Garden (2012) [FLAC]\\02 Lying In The Reeds.flac"


@pytest.fixture
def slskd(monkeypatch):
    client = SlskdClient(base_url="http://mock:5030", api_key="dummy_key")
    fake = FakeSlskd026().install(monkeypatch, client)
    return client, fake


def transfer(fake, transfer_id, state, user="DJ Peer"):
    record = {"id": transfer_id, "username": user, "direction": "Download", "filename": SONG, "size": 1000,
              "state": state, "requestedAt": iso(fake.wall)}
    fake.transfers.append(record)
    return record


def test_cancel_stops_a_waiting_transfer_and_keeps_its_record(slskd):
    client, fake = slskd
    record = transfer(fake, "queued-1", "Queued, Remotely")

    client.cancel_download("DJ Peer", "queued-1")

    assert fake.calls("DELETE") == [("DELETE", "/api/v0/transfers/downloads/DJ%20Peer/queued-1", None)]
    assert fake.cancelled == [("DJ Peer", "queued-1")]
    assert record["state"] == "Completed, Cancelled" and record["endedAt"] == iso(fake.wall)
    assert fake.transfers == [record]
    # The cancellation is discogseek's own: it does not block the peer.
    assert PeerPolicy().observe(client.get_downloads()) == {}


def test_cancel_of_a_completed_transfer_keeps_its_state(slskd):
    client, fake = slskd
    record = transfer(fake, "timed-out", "Completed, TimedOut")

    client.cancel_download("DJ Peer", "timed-out")

    assert record["state"] == "Completed, TimedOut"
    assert fake.cancelled == [("DJ Peer", "timed-out")]


def test_cancel_with_remove_drops_the_record(slskd):
    client, fake = slskd
    transfer(fake, "queued-1", "Queued, Remotely")

    reply = fake.request("DELETE", "/api/v0/transfers/downloads/DJ%20Peer/queued-1", params={"remove": "true"})

    assert reply.status_code == 204 and fake.transfers == []


def test_cancel_of_an_unknown_transfer_fails(slskd):
    client, fake = slskd
    transfer(fake, "queued-1", "Queued, Remotely")

    with pytest.raises(SlskdAPIError, match="HTTP 404"):
        client.cancel_download("DJ Peer", "missing")

    assert fake.cancelled == [] and fake.transfers[0]["state"] == "Queued, Remotely"
