"""Artist downloads complete every release containing one of the artist's tracks."""

from copy import deepcopy
from importlib import import_module
from unittest.mock import Mock

import pytest

from discogseek.cli.main import main
from discogseek.core.text import normalize_text
from discogseek.services.auditor import AuditorService
from discogseek.services.soulseek import SlskdArtistScraper


TARGET_ID = "target-artist"
TARGET_NAME = "Target Artist"


def credit(name):
    artist_id = TARGET_ID if name == TARGET_NAME else normalize_text(name).replace(" ", "-")
    return [{"artist": {"id": artist_id, "name": name}, "name": name}]


def release(media=None, artist="Various Artists", title="Shared Night Signals"):
    media = media or [[
        ("Neon Memory", TARGET_NAME),
        ("Midnight Echo", "Other Artist"),
        ("Morning Light", "Third Artist"),
    ]]
    return {
        "id": "shared-release",
        "title": title,
        "artist-credit": credit(artist),
        "release-group": {"id": "shared-group", "primary-type": "Album"},
        "medium-list": [
            {"position": str(disc), "track-list": [
                {
                    "id": f"track-{disc}-{number}",
                    "number": str(number),
                    "position": str(number),
                    "title": track_title,
                    "artist-credit": credit(track_artist),
                    "recording": {"id": f"recording-{disc}-{number}", "title": track_title},
                }
                for number, (track_title, track_artist) in enumerate(tracks, 1)
            ]}
            for disc, tracks in enumerate(media, 1)
        ],
    }


def release_files(metadata):
    files = []
    for medium in metadata["medium-list"]:
        disc = int(medium["position"])
        for track in medium["track-list"]:
            artist = track["artist-credit"][0]["name"]
            filename = (
                f"Music\\Various Artists\\{metadata['title']}\\CD{disc}\\"
                f"{int(track['number']):02d} - {artist} - {track['title']}.flac"
            )
            files.append({"filename": filename, "size": 25000000, "artist": artist})
    return files


def library_tracks(metadata, positions):
    tracks = []
    for medium in metadata["medium-list"]:
        disc = int(medium["position"])
        for track in medium["track-list"]:
            if (disc, int(track["number"])) not in positions:
                continue
            filename = f"{int(track['number']):02d} {track['title']}.flac"
            tracks.append({
                "path": f"/music/Various Artists/{metadata['title']}/CD{disc}/{filename}",
                "filename": filename,
                "title": track["title"],
                "norm_title": normalize_text(track["title"]),
                "album": metadata["title"],
                "norm_album": normalize_text(metadata["title"]),
                "artists": [track["artist-credit"][0]["name"]],
                "track_number": track["number"],
                "disc_number": disc,
                "mb_track_ids": {track["id"]},
                "mb_rec_ids": {track["recording"]["id"]},
                "mb_artist_ids": {track["artist-credit"][0]["artist"]["id"]},
                "mb_release_ids": {metadata["id"]},
                "source": "local",
            })
    return tracks


class FakeSlskdClient:
    """Keep the production matching and history paths; fake only the remote API."""

    def __init__(self, files, history=None, enqueue_error=None, responses=None):
        self.files = files
        self.responses = responses
        self.history = history or []
        self.enqueue_error = enqueue_error
        self.accepted = []
        self.enqueue_calls = []
        self.searches = []

    def get_application(self):
        return {"user": {"username": "test-user"}, "server": {"state": "Connected"}}

    def get_queued_filenames(self):
        return {
            file["filename"]
            for user in self.get_downloads()
            for directory in user["directories"]
            for file in directory["files"]
        }

    def get_downloads(self):
        return self.history + [{"username": "peer", "directories": [{"files": [
            {**file, "state": "InProgress"} for file in self.accepted
        ]}]}]

    def search(self, query, **kwargs):
        self.searches.append(query)
        return {"responses": self.responses if self.responses is not None else [{
            "username": "peer", "uploadSpeed": 5000, "hasFreeUploadSlot": True,
            "queueLength": 0, "files": self.files,
        }]}

    def batch_search(self, queries, **kwargs):
        return {query: self.search(query) for query in queries}

    def browse_directories_batch(self, directories, **kwargs):
        return {
            (user, directory): [
                file for file in self.files
                if file["filename"].rsplit("\\", 1)[0] == directory
            ]
            for user, directory in directories
        }

    def enqueue_download(self, user, files):
        self.enqueue_calls.append((user, list(files)))
        if self.enqueue_error:
            raise self.enqueue_error
        self.accepted.extend(files)
        return {"status": "enqueued", "files_count": len(files)}


def run_artist(monkeypatch, metadata, positions=(), *, primary=False, overlap=False, dry_run=False,
               files=None, inventory=None, history=None, enqueue_error=None, raw_data=None, responses=None):
    raw_data = raw_data or {
        "artist": {"id": TARGET_ID, "name": TARGET_NAME},
        "releases_artist": [metadata] if primary else [],
        "releases_track_artist": [metadata] if not primary or overlap else [],
        "recordings": [],
    }
    client = FakeSlskdClient(
        release_files(metadata) if files is None else files,
        history=history, enqueue_error=enqueue_error, responses=responses,
    )
    scraper = SlskdArtistScraper(TARGET_NAME, slskd_client=client, dry_run=dry_run)
    scraper.mb_client = Mock()
    scraper.mb_client.resolve_artist_mbid.return_value = (TARGET_ID, TARGET_NAME)
    scraper.mb_client.fetch_full_discography.return_value = raw_data
    scraper.mb_client.get_release_by_id.side_effect = AssertionError("Full metadata is already loaded")
    scraper.mb_client.search_release.side_effect = AssertionError("Full metadata is already loaded")
    scanner = Mock(return_value=library_tracks(metadata, positions) if inventory is None else inventory)
    monkeypatch.setattr(AuditorService, "scan_library", scanner)

    result = scraper.run()

    scanner.assert_called_once()
    scraper.mb_client.get_release_by_id.assert_not_called()
    scraper.mb_client.search_release.assert_not_called()
    return result, client


@pytest.mark.parametrize("positions,expected_indexes", [
    ((), [0, 1, 2]),
    (((1, 1),), [1, 2]),
    (((1, 1), (1, 2), (1, 3)), []),
])
def test_artist_completes_compilation_even_when_own_track_is_present(
    monkeypatch, positions, expected_indexes,
):
    metadata = release()

    result, client = run_artist(monkeypatch, metadata, positions)

    expected = [release_files(metadata)[index]["filename"] for index in expected_indexes]
    assert [file["filename"] for file in client.accepted] == expected
    assert result["enqueued_count"] == len(expected)
    assert [file["filename"] for file in result["queued_files"]] == expected
    assert result["unresolved_releases"] == []


def test_artist_completes_guest_appearance_on_another_artists_album(monkeypatch):
    metadata = release(artist="Other Artist")

    result, client = run_artist(monkeypatch, metadata, ((1, 1),))

    assert [file["filename"] for file in client.accepted] == [
        file["filename"] for file in release_files(metadata)[1:]
    ]
    assert result["enqueued_count"] == 2


def test_artist_completes_split_primary_release_when_own_track_is_present(monkeypatch):
    metadata = release(artist="Target Artist & Other Artist")

    result, client = run_artist(monkeypatch, metadata, ((1, 1),), primary=True)

    assert [file["filename"] for file in client.accepted] == [
        file["filename"] for file in release_files(metadata)[1:]
    ]
    assert result["enqueued_count"] == 2


def test_artist_keeps_same_title_tracks_with_different_credits(monkeypatch):
    metadata = release(media=[[("Shared Memory", TARGET_NAME), ("Shared Memory", "Other Artist")]])

    result, client = run_artist(monkeypatch, metadata, ((1, 1),))

    assert [file["filename"] for file in client.accepted] == [release_files(metadata)[1]["filename"]]
    assert result["enqueued_count"] == 1
    assert result["queued_files"][0]["artist"] == "Other Artist"


def test_artist_keeps_repeated_recording_positions_on_multiple_discs(monkeypatch):
    metadata = release(media=[
        [("Shared Memory", TARGET_NAME), ("Shared Memory", "Other Artist")],
        [("Shared Memory", "Other Artist"), ("Morning Light", "Third Artist")],
    ])
    # The same recording still occupies two separate positions in this release.
    metadata["medium-list"][1]["track-list"][0]["recording"]["id"] = "recording-1-2"

    result, client = run_artist(monkeypatch, metadata, ((1, 1), (1, 2)))

    assert [file["filename"] for file in client.accepted] == [
        file["filename"] for file in release_files(metadata)[2:]
    ]
    assert result["enqueued_count"] == 2
    assert {(file["disc_number"], file["track_number"]) for file in result["queued_files"]} == {
        (2, 1), (2, 2),
    }


def test_artist_full_release_dry_run_matches_all_credits_without_enqueue(monkeypatch):
    result, client = run_artist(monkeypatch, release(), dry_run=True)

    assert client.enqueue_calls == []
    assert result["enqueued_count"] == 0
    assert sum(download["matched_count"] for download in result["release_downloads"]) == 3
    assert result["unresolved_releases"] == []


def test_artist_downloads_release_in_primary_and_appearance_catalogs_once(monkeypatch):
    metadata = release(artist="Target Artist & Other Artist")

    result, client = run_artist(monkeypatch, metadata, primary=True, overlap=True)

    assert [file["filename"] for file in client.accepted] == [
        file["filename"] for file in release_files(metadata)
    ]
    assert result["enqueued_count"] == 3
    assert len(result["release_downloads"]) == 1


@pytest.mark.parametrize("dry_run", [False, True])
def test_artist_incomplete_compilation_only_matches_own_tracks(monkeypatch, dry_run):
    metadata = release()
    files = release_files(metadata)

    result, client = run_artist(monkeypatch, metadata, files=files[:2], dry_run=dry_run)

    assert [file["filename"] for file in client.accepted] == ([] if dry_run else [files[0]["filename"]])
    download = result["release_downloads"][0]
    assert [file["filename"] for file in download["queued_files"]] == [files[0]["filename"]]
    assert download["download_scope"] == "artist_tracks"
    assert result["unresolved_releases"] == []
    assert not any("Other Artist - Midnight Echo" in query for query in client.searches)


def test_artist_full_release_cli_dry_run_lists_all_matched_files(monkeypatch, capsys):
    metadata = release()
    result, client = run_artist(monkeypatch, metadata, dry_run=True)
    monkeypatch.setattr(
        import_module("discogseek.cli.main"), "SlskdArtistScraper",
        Mock(return_value=Mock(run=Mock(return_value=result))),
    )

    exit_code = main(["download", TARGET_NAME, "--dry-run", "--quiet"])

    output = capsys.readouterr()
    assert exit_code == 0
    assert "3 matched files (dry run; no transfers queued)" in output.out
    assert output.out.count("MATCH |") == 3
    for file in release_files(metadata):
        assert f"MATCH | {file['filename']}" in output.out
    assert client.enqueue_calls == []


def test_artist_full_release_enqueue_failure_reaches_cli(monkeypatch, capsys):
    result, client = run_artist(
        monkeypatch, release(), enqueue_error=RuntimeError("queue backend unavailable"),
    )
    monkeypatch.setattr(
        import_module("discogseek.cli.main"), "SlskdArtistScraper",
        Mock(return_value=Mock(run=Mock(return_value=result))),
    )

    exit_code = main(["download", TARGET_NAME, "--quiet"])

    output = capsys.readouterr()
    assert client.enqueue_calls
    assert client.accepted == []
    assert result["enqueued_count"] == 0
    assert result["queued_files"] == []
    assert any("queue backend unavailable" in error for error in result["queue_errors"])
    assert exit_code == 1
    assert "queue backend unavailable" in output.err
    assert "0 files queued through slskd" in output.out
    assert "MATCH |" not in output.out


@pytest.mark.parametrize("state", ["Queued, Remotely", "InProgress", "Completed, Succeeded"])
def test_artist_full_release_preserves_other_artists_healthy_transfers(monkeypatch, state):
    metadata = release()
    files = release_files(metadata)
    history = [{"username": "earlier-peer", "directories": [{"files": [
        {**files[1], "filename": files[1]["filename"].replace(".flac", ".mp3"), "state": state},
    ]}]}]

    result, client = run_artist(monkeypatch, metadata, history=history)

    assert [file["filename"] for file in client.accepted] == [files[0]["filename"], files[2]["filename"]]
    assert result["enqueued_count"] == 2
    assert result["queued_files"] == client.accepted
    assert result["unresolved_releases"] == []
    assert result["queue_errors"] == []


def test_artist_full_release_skips_other_artists_track_present_in_navidrome(monkeypatch):
    metadata = release()
    inventory = library_tracks(metadata, ((1, 1), (1, 2)))
    inventory[1].update(source="navidrome", path="", filename="")

    result, client = run_artist(monkeypatch, metadata, inventory=inventory)

    assert [file["filename"] for file in client.accepted] == [release_files(metadata)[2]["filename"]]
    assert result["enqueued_count"] == 1
    assert result["unresolved_releases"] == []


def test_artist_downloads_different_compilation_groups_with_the_same_title(monkeypatch):
    first = release()
    second = release(media=[[("Dawn Voices", TARGET_NAME), ("Purple Skies", "Fourth Artist")]])
    second["id"] = "second-release"
    second["release-group"]["id"] = "second-group"
    for track in second["medium-list"][0]["track-list"]:
        track["id"] = "second-" + track["id"]
        track["recording"]["id"] = "second-" + track["recording"]["id"]
    raw_data = {
        "artist": {"id": TARGET_ID, "name": TARGET_NAME},
        "releases_artist": [], "releases_track_artist": [first, second], "recordings": [],
    }
    files = release_files(first) + release_files(second)

    result, client = run_artist(monkeypatch, first, raw_data=raw_data, files=files)

    assert [file["filename"] for file in client.accepted] == [file["filename"] for file in files]
    assert result["enqueued_count"] == 5
    assert {download["release_id"] for download in result["release_downloads"]} == {
        "shared-release", "second-release",
    }
    assert result["unresolved_releases"] == []


def test_artist_processes_multiple_appearance_editions_of_one_group_once(monkeypatch):
    first = release()
    second = deepcopy(first)
    second.update(id="second-edition", title="Shared Night Signals Expanded")
    second["medium-list"][0]["track-list"].append({
        "id": "bonus-track", "number": "4", "position": "4", "title": "Bonus Memory",
        "artist-credit": credit(TARGET_NAME),
        "recording": {"id": "bonus-recording", "title": "Bonus Memory"},
    })
    raw_data = {
        "artist": {"id": TARGET_ID, "name": TARGET_NAME},
        "releases_artist": [], "releases_track_artist": [first, second], "recordings": [],
    }

    result, client = run_artist(monkeypatch, first, raw_data=raw_data)

    assert [file["filename"] for file in client.accepted] == [
        file["filename"] for file in release_files(first)
    ]
    assert result["enqueued_count"] == 3
    assert len(result["release_downloads"]) == 1
    assert result["release_downloads"][0]["release_id"] == first["id"]
    assert result["unresolved_releases"] == []


@pytest.mark.parametrize("track_artist,expected_indexes", [
    ("Unrelated Band", [1, 2]),
    ("Other Artist", [2]),
])
def test_artist_scopes_partially_tagged_same_title_albums_by_artist(
    monkeypatch, track_artist, expected_indexes,
):
    metadata = release()
    inventory = library_tracks(metadata, ((1, 1), (1, 2)))
    inventory[1].update(
        album_artist=track_artist, artists=[track_artist], mb_artist_ids=set(),
        mb_release_ids=set(), mb_rec_ids=set(), mb_track_ids=set(),
    )

    result, client = run_artist(monkeypatch, metadata, inventory=inventory)

    files = release_files(metadata)
    assert [file["filename"] for file in client.accepted] == [
        files[index]["filename"] for index in expected_indexes
    ]
    assert result["enqueued_count"] == len(expected_indexes)
    assert result["unresolved_releases"] == []


def test_artist_prefers_complete_compilation_over_higher_quality_partial_source(monkeypatch):
    metadata = release()
    files = release_files(metadata)
    complete_files = [{**file, "filename": file["filename"].replace(".flac", ".mp3"), "bitRate": 320}
                      for file in files]
    responses = [
        {"username": "partial-flac", "files": files[:2]},
        {"username": "complete-mp3", "files": complete_files},
    ]

    result, client = run_artist(monkeypatch, metadata, responses=responses)

    assert {user for user, _ in client.enqueue_calls} == {"complete-mp3"}
    assert [file["filename"] for file in client.accepted] == [file["filename"] for file in complete_files]
    assert result["release_downloads"][0]["download_scope"] == "release"
    assert result["unresolved_releases"] == []


@pytest.mark.parametrize("split", ["peers", "folders"])
def test_artist_does_not_assemble_complete_compilation_from_partial_sources(monkeypatch, split):
    metadata = release()
    files = release_files(metadata)
    if split == "peers":
        responses = [{"username": "peer-a", "files": files[:2]},
                     {"username": "peer-b", "files": files[2:]}]
    else:
        files = [{**file, "filename": file["filename"].replace(
            "\\CD1\\", f"\\Edition {'A' if index < 2 else 'B'}\\CD1\\",
        )} for index, file in enumerate(files)]
        responses = [{"username": "peer", "files": files}]

    result, client = run_artist(monkeypatch, metadata, responses=responses)

    assert [file["filename"] for file in client.accepted] == [files[0]["filename"]]
    assert result["release_downloads"][0]["download_scope"] == "artist_tracks"
    assert result["unresolved_releases"] == []


def test_artist_missing_disc_restricts_compilation_to_own_tracks(monkeypatch):
    metadata = release(media=[
        [("Neon Memory", TARGET_NAME), ("Midnight Echo", "Other Artist")],
        [("Morning Light", "Third Artist")],
    ])
    files = release_files(metadata)

    result, client = run_artist(monkeypatch, metadata, files=files[:2])

    assert [file["filename"] for file in client.accepted] == [files[0]["filename"]]
    assert result["release_downloads"][0]["download_scope"] == "artist_tracks"


@pytest.mark.parametrize("available_indexes", [[0, 1], [1, 2]])
def test_artist_incomplete_source_never_queues_others_when_own_track_is_present(monkeypatch, available_indexes):
    metadata = release()
    files = release_files(metadata)

    result, client = run_artist(monkeypatch, metadata, ((1, 1),),
                                files=[files[index] for index in available_indexes])

    assert client.enqueue_calls == []
    assert result["enqueued_count"] == 0
    assert result["unresolved_releases"] == []


@pytest.mark.parametrize("credit_kind", ["alias", "collaboration", "recording"])
def test_artist_compilation_fallback_uses_exact_artist_credit_ids(monkeypatch, credit_kind):
    metadata = release()
    own, other, _ = metadata["medium-list"][0]["track-list"]
    # A different artist with the same credited display name must not enter fallback.
    other["artist-credit"] = [{"name": TARGET_NAME, "artist": {"id": "namesake", "name": TARGET_NAME}}]
    if credit_kind == "alias":
        own["artist-credit"][0]["name"] = "Target Alias"
    elif credit_kind == "collaboration":
        own["artist-credit"][0]["joinphrase"] = " & "
        own["artist-credit"] += credit("Collaborator")
    files = release_files(metadata)
    if credit_kind == "collaboration":
        files[0]["artist"] = f"{TARGET_NAME} & Collaborator"
        files[0]["filename"] = files[0]["filename"].replace(TARGET_NAME, files[0]["artist"])
    if credit_kind == "recording":
        own["recording"]["artist-credit"] = own.pop("artist-credit")

    result, client = run_artist(monkeypatch, metadata, files=files[:2])

    assert [file["filename"] for file in client.accepted] == [files[0]["filename"]]
    assert result["release_downloads"][0]["download_scope"] == "artist_tracks"
    assert result["unresolved_releases"] == []
