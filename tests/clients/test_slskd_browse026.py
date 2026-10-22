"""SlskdClient directory browsing against slskd 0.26 replies (tests/helpers/slskd026.py)."""

import pytest

from discogseek.clients.slskd import SlskdClient
from tests.helpers.slskd026 import FakeSlskd026

ALBUM = "music\\Lone - Reality Testing (japanese edition) (2014) [CD FLAC]"
TRACKS = ["01 First Born Seconds.flac", "02 Restless City.flac", "03 Meeker Warm Energy.flac"]


@pytest.fixture
def slskd(monkeypatch):
    client = SlskdClient(base_url="http://mock:5030", api_key="dummy_key")
    fake = FakeSlskd026().install(monkeypatch, client)
    return client, fake


def test_browse_returns_files_with_full_remote_paths(slskd):
    client, fake = slskd
    fake.add_directory("estrogenpanther", ALBUM, *TRACKS, "cover.jpg", size=2000)

    files = client.browse_directory("estrogenpanther", ALBUM)

    assert [file["filename"] for file in files] == [f"{ALBUM}\\{name}" for name in TRACKS + ["cover.jpg"]]
    assert all(file["size"] == 2000 and file["code"] == 1 for file in files)


def test_browse_keeps_only_the_requested_directory(slskd):
    client, fake = slskd
    fake.add_directory("estrogenpanther", ALBUM, *TRACKS)
    fake.add_directory("estrogenpanther", f"{ALBUM}\\Scans", "booklet 01.jpg")

    files = client.browse_directory("estrogenpanther", ALBUM)

    assert [file["filename"] for file in files] == [f"{ALBUM}\\{name}" for name in TRACKS]


def test_browse_matches_the_requested_directory_regardless_of_separators_and_case(slskd):
    client, fake = slskd
    fake.add_directory("agressivelyOrange", "shared/music/Lone/Reality Testing (2014)", "01 - First Born Seconds.flac")
    fake.add_directory("agressivelyOrange", "shared/music/Lone/Reality Testing (2014)\\CD2", "01 - Bonus.flac")
    original = fake.request

    def peer_spelling(method, path, **kwargs):
        if "directory" in path:
            kwargs["json"] = {"directory": "shared/music/Lone/Reality Testing (2014)"}
        return original(method, path, **kwargs)

    client._request = peer_spelling
    files = client.browse_directory("agressivelyOrange", "shared\\music\\lone\\reality testing (2014)\\")

    assert [file["filename"] for file in files] == [
        "shared/music/Lone/Reality Testing (2014)/01 - First Born Seconds.flac",
    ]


def test_browse_keeps_every_directory_when_none_is_the_requested_one(slskd):
    client, fake = slskd
    fake.add_directory("HarmoniciGelignite", "@@music\\Electronic\\Urban Biology", "01 Cream Soda.flac")
    fake.add_directory("HarmoniciGelignite", "@@music\\Electronic\\Urban Biology\\Bonus", "11 Icya.flac")
    original = fake.request

    def peer_spelling(method, path, **kwargs):
        # The peer answers with its own share prefix.
        if "directory" in path:
            kwargs["json"] = {"directory": "@@music\\Electronic\\Urban Biology"}
        return original(method, path, **kwargs)

    client._request = peer_spelling
    files = client.browse_directory("HarmoniciGelignite", "Electronic\\Urban Biology")

    assert [file["filename"] for file in files] == [
        "@@music\\Electronic\\Urban Biology\\01 Cream Soda.flac",
        "@@music\\Electronic\\Urban Biology\\Bonus\\11 Icya.flac",
    ]


def test_browse_accepts_a_single_directory_and_flat_listings(monkeypatch):
    client = SlskdClient(base_url="http://mock:5030", api_key="dummy_key")
    replies = [
        {"name": ALBUM, "fileCount": 1, "files": [{"filename": TRACKS[0], "size": 1}]},
        [{"filename": f"{ALBUM}\\{TRACKS[1]}", "size": 2}],
    ]

    class Reply:
        status_code = 200

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: Reply(replies.pop(0)))

    assert client.browse_directory("peer", ALBUM, use_cache=False) == [{"filename": f"{ALBUM}\\{TRACKS[0]}", "size": 1}]
    assert client.browse_directory("peer", ALBUM, use_cache=False) == [{"filename": f"{ALBUM}\\{TRACKS[1]}", "size": 2}]


@pytest.mark.parametrize("username, encoded", [
    ("GotRoot?", "GotRoot%3F"),
    ("IsaBelle44??", "IsaBelle44%3F%3F"),
    ("Carnage \\m/", "Carnage%20%5Cm%2F"),
    ("a#b", "a%23b"),
    ("50%off", "50%25off"),
])
def test_browse_url_encodes_the_username(slskd, username, encoded):
    client, fake = slskd
    fake.add_directory(username, ALBUM, *TRACKS)

    files = client.browse_directory(username, ALBUM)

    assert len(files) == 3
    assert fake.calls("POST")[0][1] == f"/api/v0/users/{encoded}/directory"


def test_browse_cache_holds_the_flattened_listing(slskd):
    client, fake = slskd
    fake.add_directory("estrogenpanther", ALBUM, *TRACKS)

    first = client.browse_directory("estrogenpanther", ALBUM)
    batch = client.browse_directories_batch([("estrogenpanther", ALBUM)])

    assert batch[("estrogenpanther", ALBUM)] == first
    assert first[0]["filename"] == f"{ALBUM}\\{TRACKS[0]}"
    assert len(fake.calls("POST")) == 1


def test_artist_mode_browse_expands_a_partial_search_folder(slskd):
    """Phase 2 must see every file of a folder whose search hit only one track."""
    from discogseek.clients.musicbrainz import ArtistCatalog
    from discogseek.services.soulseek import PeerCandidateIndex, SlskdArtistScraper

    client, fake = slskd
    fake.add_directory("estrogenpanther", ALBUM, *TRACKS)
    raw = {
        "artist": {"id": "lone", "name": "Lone"},
        "releases_artist": [{"id": "reality-testing", "title": "Reality Testing", "medium-list": [{"track-list": [
            {"id": f"t{n}", "title": title} for n, title in
            enumerate(["First Born Seconds", "Restless City", "Meeker Warm Energy"], 1)
        ]}]}],
        "releases_track_artist": [], "recordings": [],
    }
    scraper = SlskdArtistScraper(artist_query="Lone", dry_run=True)
    scraper.catalog = ArtistCatalog(raw)
    scraper.all_artist_aliases = {"lone"}
    scraper.client = client
    scraper.peer_directories = {("estrogenpanther", ALBUM): {
        "user": "estrogenpanther", "directory": ALBUM, "speed": 1000, "queue": 0, "has_slot": True,
        "matched_search_files": [{"filename": f"{ALBUM}\\{TRACKS[0]}", "size": 1000}],
        "full_directory_files": None,
    }}
    scraper.candidate_index = PeerCandidateIndex(scraper.peer_directories)

    scraper._reconcile_primary_releases()

    assert len(fake.calls("POST", "/api/v0/users/")) == 1
    assert [(release["matched_count"], release["total_count"]) for release in scraper.verified_releases] == [(3, 3)]
