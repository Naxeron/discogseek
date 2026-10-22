"""A transfer stands for the row it was submitted for, wherever its folder is (core.submissions).

Seen in a real run: the 12" 'Modeselektion Vol.04 #2' was downloaded from the folder of the
2CD edition it was cut from. Once those transfers had finished, their folder no longer named
the release, and the next run downloaded the same four rows again from another peer.
"""

from unittest.mock import MagicMock

import pytest

from discogseek.config import Config
from discogseek.core.submissions import SubmissionLog
from discogseek.services.library_download import download_missing_tracks
from tests.helpers.slskd026 import iso, peer
from tests.services.test_release_download_slskd026 import slskd  # noqa: F401  (fixture)

RELEASE = "Modeselektion Vol.04 #2"
QUERY = "Modeselektion Vol.04 2"
PARENT = r"music\Modeselektor - Modeselektion, Vol. 04 (2018) [FLAC] {MTR089CD}"
MATTE = PARENT + r"\Disc 2\05 - FJAAK - Matte.flac"
INTRODUCTION = PARENT + r"\Disc 2\01 - Radio Slave - Introduction.flac"
SCENE = r"@ Flac @\= Techno =\VA-Modeselektion_Vol._04-(MTR089CD)-2CD-FLAC-2018-HOUND"
ROWS = [
    {"title": "Matte", "artist": "FJAAK", "disc_number": 1, "track_number": "A2", "position_token": "A2",
     "mb_track_id": "7144eaec-3473-4d0b-93a4-04f4c1e8f337", "status": "missing"},
    {"title": "Introduction", "artist": "Radio Slave", "disc_number": 1, "track_number": "B1",
     "position_token": "B1", "mb_track_id": "f978616d-2fcc-4743-a638-c33216c16c16", "status": "missing"},
]


def run(client, rows=ROWS, **kwargs):
    return download_missing_tracks(client, MagicMock(), "Various Artists", RELEASE, [dict(row) for row in rows],
                                   **kwargs)


def log_path():
    return Config.CACHE_DIR / "submissions.db"


def queue_from_parent_folder(client, fake, tmp_path, rows=ROWS):
    """Run 1 queues the rows from the 2CD folder; the files finish and wait in slskd's downloads folder."""
    fake.downloads_dir = str(tmp_path / "downloads")
    fake.plan_search(QUERY, [peer("estrogenpanther", MATTE, INTRODUCTION)])

    first = run(client, rows=rows)

    assert fake.enqueued() == [("estrogenpanther", MATTE), ("estrogenpanther", INTRODUCTION)]
    assert first["queued_tracks"] == rows
    for transfer in fake.transfers:
        transfer.update(state="Completed, Succeeded", endedAt=iso(fake.wall))
    local = tmp_path / "downloads" / "Disc 2"
    local.mkdir(parents=True)
    for name in ("05 - FJAAK - Matte.flac", "01 - Radio Slave - Introduction.flac"):
        (local / name).write_bytes(b"")
    # Another peer now answers the release search with the scene release.
    fake.searches.clear()
    fake.plan_search(QUERY, [peer("chuckieladdie", SCENE + r"\205-fjaak-matte.flac",
                                  SCENE + r"\201-radio_slave-introduction.flac")])


@pytest.fixture
def first_run(slskd, monkeypatch, tmp_path):  # noqa: F811
    client, fake = slskd
    monkeypatch.delenv("DISCOGSEEK_SLSKD_DOWNLOADS_DIR")
    queue_from_parent_folder(client, fake, tmp_path)
    return client, fake


def test_finished_files_from_the_parent_releases_folder_are_not_downloaded_again(first_run):
    client, fake = first_run

    second = run(client)

    assert [user for user, _ in fake.enqueued()] == ["estrogenpanther", "estrogenpanther"]
    assert second["queued_tracks"] == ROWS and second["enqueued_files"] == []
    assert {(file["user"], file["filename"]) for file in second["queued_files"]} == {
        ("estrogenpanther", MATTE), ("estrogenpanther", INTRODUCTION),
    }
    # Both rows stood for themselves before any search: only run 1 searched.
    assert fake.search_posts() == [QUERY]


def test_without_the_log_a_finished_file_needs_the_releases_own_folder(first_run):
    client, fake = first_run
    log_path().unlink()

    second = run(client)

    assert [user for user, _ in fake.enqueued()] == ["estrogenpanther"] * 2 + ["chuckieladdie"] * 2
    assert second["queued_tracks"] == ROWS


def test_a_file_submitted_for_another_row_does_not_stand_for_this_one(first_run):
    client, fake = first_run
    log_path().unlink()
    # The same files were submitted for another release's rows (the 2CD edition's).
    SubmissionLog().record([("estrogenpanther", MATTE, "track:2cd-matte"),
                            ("estrogenpanther", INTRODUCTION, "track:2cd-introduction")])

    second = run(client)

    assert [user for user, _ in fake.enqueued()] == ["estrogenpanther"] * 2 + ["chuckieladdie"] * 2
    assert second["queued_tracks"] == ROWS


def test_rows_without_a_musicbrainz_track_id_are_told_apart_by_release_and_position(slskd, monkeypatch,  # noqa: F811
                                                                                     tmp_path):
    client, fake = slskd
    monkeypatch.delenv("DISCOGSEEK_SLSKD_DOWNLOADS_DIR")
    rows = [{key: value for key, value in row.items() if key != "mb_track_id"} for row in ROWS]
    queue_from_parent_folder(client, fake, tmp_path, rows=rows)

    second = run(client, rows=rows)

    assert len(fake.enqueued()) == 2 and second["queued_tracks"] == rows and second["enqueued_files"] == []
    # Another position of the release is another row.
    moved = [dict(rows[0], track_number="A3", position_token="A3")]
    third = run(client, rows=moved)
    assert fake.enqueued()[2:] == [("chuckieladdie", SCENE + r"\205-fjaak-matte.flac")]
    assert third["queued_tracks"] == moved


def test_dry_runs_never_write_the_log(slskd):  # noqa: F811
    client, fake = slskd
    fake.plan_search(QUERY, [peer("estrogenpanther", MATTE, INTRODUCTION)])

    preview = run(client, dry_run=True)

    assert len(preview["queued_files"]) == 2 and fake.enqueued() == []
    assert not log_path().exists()


def test_a_kept_live_transfer_is_logged_for_the_row_it_stands_for(slskd):  # noqa: F811
    client, fake = slskd
    named = r"Share\VA - Modeselektion Vol.04 #2\A2 - FJAAK - Matte.flac"
    fake.seed("collector", named, "Queued, Remotely", age=600)

    kept = run(client, rows=ROWS[:1])

    assert fake.enqueued() == [] and kept["queued_tracks"] == ROWS[:1]
    assert SubmissionLog().identities([("collector", named)]) == {
        ("collector", named.replace("\\", "/")): {"track:" + ROWS[0]["mb_track_id"]},
    }


# A live 'Intro' queued for another release is that release's song (c4: First Light / Phonetic Dreams).
INTRO = {"title": "Intro", "artist": "Lone", "track_number": 1, "disc_number": 1,
         "mb_track_id": "phonetic-dreams-intro", "status": "missing"}
FIRST_LIGHT_INTRO = r"Music\Lone\First Light\01 Intro.flac"
OWN_INTRO = r"Share\Lone\Phonetic Dreams\01 Intro.flac"


@pytest.mark.parametrize("logged_for, expected", [
    ("track:phonetic-dreams-intro", []),
    ("track:first-light-intro", [("own", OWN_INTRO)]),
    (None, [("own", OWN_INTRO)]),
])
def test_live_transfer_stands_for_the_row_it_was_submitted_for(slskd, logged_for, expected):  # noqa: F811
    client, fake = slskd
    fake.seed("artist-run", FIRST_LIGHT_INTRO, "Queued, Remotely", age=600)
    if logged_for:
        SubmissionLog().record([("artist-run", FIRST_LIGHT_INTRO, logged_for)])
    fake.plan_search("Lone Intro", [peer("own", OWN_INTRO)])

    result = download_missing_tracks(client, MagicMock(), "Lone", "Phonetic Dreams", [dict(INTRO)],
                                     search_scope="track")

    assert fake.enqueued() == expected
    assert result["queued_tracks"] == [INTRO]
    if not expected:
        assert [(file["user"], file["filename"]) for file in result["queued_files"]] == [
            ("artist-run", FIRST_LIGHT_INTRO),
        ]


def test_an_unreadable_log_falls_back_to_the_folder_rules(first_run):
    client, fake = first_run
    log_path().write_bytes(b"not a database" * 100)

    second = run(client)

    assert [user for user, _ in fake.enqueued()] == ["estrogenpanther"] * 2 + ["chuckieladdie"] * 2
    assert second["queued_tracks"] == ROWS


def test_another_artists_release_of_the_same_title_is_another_row(slskd, monkeypatch, tmp_path):  # noqa: F811
    """Without MusicBrainz IDs two artists' 'Greatest Hits' share a title, a position and an 'Intro'."""
    client, fake = slskd
    monkeypatch.delenv("DISCOGSEEK_SLSKD_DOWNLOADS_DIR")
    fake.downloads_dir = str(tmp_path / "downloads")
    alpha = r"Music\Alpha\Greatest Hits\01 Intro.flac"
    beta = r"Music\Beta\Greatest Hits\01 Intro.flac"
    row = {"title": "Intro", "track_number": 1, "disc_number": 1, "status": "missing"}
    fake.plan_search("Alpha Greatest Hits", [peer("alpha-fan", alpha)])

    download_missing_tracks(client, MagicMock(), "Alpha", "Greatest Hits", [dict(row, artist="Alpha")])

    assert fake.enqueued() == [("alpha-fan", alpha)]
    # Alpha's file finishes and waits for its import.
    for transfer in fake.transfers:
        transfer.update(state="Completed, Succeeded", endedAt=iso(fake.wall))
    local = tmp_path / "downloads" / "Greatest Hits"
    local.mkdir(parents=True)
    (local / "01 Intro.flac").write_bytes(b"")
    fake.plan_search("Beta Greatest Hits", [peer("beta-fan", beta)])

    second = download_missing_tracks(client, MagicMock(), "Beta", "Greatest Hits", [dict(row, artist="Beta")])

    assert fake.enqueued()[1:] == [("beta-fan", beta)]
    assert [(file["user"], file["filename"]) for file in second["queued_files"]] == [("beta-fan", beta)]


def test_a_logged_radio_capture_never_stands_for_its_row(slskd, monkeypatch, tmp_path):  # noqa: F811
    # Seen in a real run: an earlier version took 'Friends And Lovers' from a SomaFM channel
    # capture. Its log entry must not keep standing for the row once such folders are refused.
    client, fake = slskd
    monkeypatch.delenv("DISCOGSEEK_SLSKD_DOWNLOADS_DIR")
    fake.downloads_dir = str(tmp_path / "downloads")
    radio = r"Out\DEF CON Radio  SomaFM's year-round channel for DEF CON [SomaFM]\Shigeto - Friends And Lovers.mp3"
    fake.transfers.append({
        "id": "transfer-radio", "username": "nofocus", "direction": "Download", "filename": radio,
        "size": 1000, "state": "Completed, Succeeded", "requestedAt": iso(fake.wall), "endedAt": iso(fake.wall),
    })
    local = tmp_path / "downloads" / "DEF CON Radio  SomaFM's year-round channel for DEF CON [SomaFM]"
    local.mkdir(parents=True)
    (local / "Shigeto - Friends And Lovers.mp3").write_bytes(b"")
    row = {"title": "Friends And Lovers", "artist": "Shigeto", "disc_number": 1, "track_number": "1",
           "mb_track_id": "cambio-friends-and-lovers", "status": "missing"}
    SubmissionLog().record([("nofocus", radio, "track:cambio-friends-and-lovers")])
    album = r"Music\Shigeto\Cambio\01 Friends And Lovers.mp3"
    fake.plan_search("Cambio", [peer("xLouie", album)])

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "Cambio", [dict(row)])

    assert fake.enqueued() == [("xLouie", album)]
    assert [file["user"] for file in result["queued_files"]] == ["xLouie"]


BALANCE = r"Music\Balance 014 (Mixed by James Holden)"
BALANCE_ALICE = BALANCE + r"\CD1\03 - Nathan Fake - Lights of Color.flac"
BALANCE_BOB = r"Rips\Balance 014 (Mixed by James Holden)\CD1\03 - Nathan Fake - Lights of Colour.mp3"


def test_a_logged_transfer_from_a_mix_releases_own_folder_still_stands(slskd, monkeypatch, tmp_path):  # noqa: F811
    # A DJ-mix release's own folder is not a 'mix source' for that release: its log entry keeps
    # standing for the row, so a second peer's copy is not downloaded as well.
    client, fake = slskd
    monkeypatch.delenv("DISCOGSEEK_SLSKD_DOWNLOADS_DIR")
    fake.downloads_dir = str(tmp_path / "downloads")
    row = {"title": "Lights of Colour", "artist": "Nathan Fake", "disc_number": 1, "track_number": "3",
           "mb_track_id": "balance-014-3", "status": "missing"}
    fake.plan_search("Balance 014", [peer("alice", BALANCE_ALICE)])
    download_missing_tracks(client, MagicMock(), "Various Artists", "Balance 014", [dict(row)])
    assert fake.enqueued() == [("alice", BALANCE_ALICE)]
    for transfer in fake.transfers:
        transfer.update(state="Completed, Succeeded", endedAt=iso(fake.wall))
    local = tmp_path / "downloads" / "CD1"
    local.mkdir(parents=True)
    (local / "03 - Nathan Fake - Lights of Color.flac").write_bytes(b"")
    fake.searches.clear()
    fake.plan_search("Balance 014", [peer("bob", BALANCE_BOB)])

    download_missing_tracks(client, MagicMock(), "Various Artists", "Balance 014", [dict(row)])

    assert fake.enqueued() == [("alice", BALANCE_ALICE)]
