"""Peer copies the matcher misread while completing Rotator's and Repeater's discographies."""

from unittest.mock import MagicMock

import pytest

from discogseek.core.text import title_match_score, underscore_position_field
from discogseek.services.library_download import _filename_segments, download_missing_tracks


def client_answering(answers):
    """A client whose searches return (user, path, length) files only for the queries in `answers`."""
    client = MagicMock()
    client.get_downloads.return_value = []

    def results(query):
        by_user = {}
        for user, path, length in answers.get(query, []):
            by_user.setdefault(user, []).append({"filename": path, "size": 5_000_000, "length": length})
        return {"state": "Completed", "isComplete": True, "responses": [
            {"username": user, "hasFreeUploadSlot": True, "queueLength": 0, "uploadSpeed": 1, "files": files}
            for user, files in by_user.items()]}

    client.search.side_effect = lambda query=None, **kwargs: results(query)
    client.batch_search.side_effect = lambda queries, **kwargs: {query: results(query) for query in queries}
    return client


NOVO_DUB = "Alternative Novo-Dub, Volume 2: Clash and Diversion"
NOVO_DUB_TRACKS = [
    ("Duboratory's Theme", "Munky Lee", 312),
    ("Alternative Dub Xperience", "GG Project", 341),
    ("Melvana No Way Style Mix", "High Tone feat. Caracas", 273),
    ("2 Thinghs Before You Leave", "Repeater", 401),
]


def novo_dub_rows():
    return [
        {"title": title, "artist": artist, "disc_number": 1, "track_number": str(number), "duration": length,
         "mb_track_id": f"novo-{number}", "status": "missing"}
        for number, (title, artist, length) in enumerate(NOVO_DUB_TRACKS, 1)
    ]


def test_underscore_fields_in_a_spaced_name_give_position_and_title():
    # 'Artist_Album_NN_Title': the album part has spaces and a ' - ' of its own.
    stem = "Various Artists_Alternative Novo-Dub, Volume 2 - Clash and Diversion_01_Duboratory's Theme"

    assert _filename_segments(stem)[-2:] == ["01", "Duboratory's Theme"]
    assert title_match_score("Duboratory's Theme", stem + ".mp3") == 1.0


@pytest.mark.parametrize("stem", [
    "05_lone_big_world",                 # underscores stand for spaces
    "Rotator_-_HXC_Militia_320",
    "01_Rotator-Ole_Olew_Junkyard_Rmx_(Original_Mix)",
    "Artist - Song_2_Mix",               # a title with an underscored number
    # Real library names: underscores replaced '/', ':' or spaces inside the title.
    "64 Squarepusher - Iambic_9_Poetry (KenKoTaiji Remix)",
    "03 PANTSU SCUM - 10_8_2018 (Vaenus remix)",
    "12 tobokegao - LSDj v8_2_2 Bコマンドテスト",
    "13 Jizzzle - Nameless_4_2_final_2_def_new",
    "Kid Loco - Studio_54_Nights",
    "2019_05_12 Live at Fabric",
    "Rotator_-_Ole Olew Junkyard_-_03_-_Gabba Track",  # an '_-_' template splits on its own
])
def test_names_writing_spaces_or_title_characters_as_underscores_keep_their_reading(stem):
    assert underscore_position_field(stem) == stem


def test_titles_with_underscored_numbers_keep_matching_only_themselves():
    assert title_match_score("Iambic 9 Poetry (KenKoTaiji Remix)",
                             "64 Squarepusher - Iambic_9_Poetry (KenKoTaiji Remix).flac") == 1.0
    assert title_match_score("Nights", "Kid Loco - Studio_54_Nights.mp3") < 0.8
    assert _filename_segments("Rotator_-_Ole Olew Junkyard_-_03_-_Gabba Track")[-1] == "Gabba Track"


@pytest.mark.parametrize("stem, title", [
    ("Venetian Snares_7sevens.med_03_More Drugs Less Love", "More Drugs Less Love"),
    ("RADWIMPS_君の名は。 Original Soundtrack_01_夢灯籠", "夢灯籠"),
])
def test_other_underscore_field_names_read_their_title(stem, title):
    assert _filename_segments(stem)[-1] == title


def test_complete_compilation_named_artist_album_position_title_is_verified():
    folder = r"music\Various Artists\Alternative Novo-Dub, Volume 2 - Clash and Diversion [2001]"
    files = [
        ("jussij", rf"{folder}\Various Artists_Alternative Novo-Dub, Volume 2 - Clash and Diversion_{number:02d}_{title}.mp3",
         length - 1)
        for number, (title, _, length) in enumerate(NOVO_DUB_TRACKS, 1)
    ]
    client = client_answering({"Alternative Novo-Dub Volume 2 Clash and Diversion": files})
    rows = novo_dub_rows()

    result = download_missing_tracks(client, MagicMock(), "Various Artists", NOVO_DUB, rows,
                                     dry_run=True, complete_release_tracks=rows)

    assert result["complete_release_available"]
    assert result["matched_count"] == len(NOVO_DUB_TRACKS)
    assert [file["filename"] for file in result["queued_files"]] == [path for _, path, _ in files]


@pytest.mark.parametrize("scope", ["track", "release"])
def test_parts_holding_a_releases_words_out_of_order_do_not_name_it(scope):
    # 'Hardcore Remixes Vol 2' + 'Repeater' holds the words of 'Repeater Remixes'.
    row = {"title": "Some Song (X Remix)", "artist": "Repeater", "track_number": "3", "duration": 300.0,
           "status": "missing", "mb_track_id": "t3"}
    path = r"music\VA - Hardcore Remixes Vol 2\VA - Hardcore Remixes Vol 2 - Repeater - Some Song (X Remix).mp3"
    client = client_answering({query: [("peer", path, 200)] for query in (
        "Repeater Repeater Remixes", "Repeater Remixes", "Repeater Some Song (X Remix)", "Repeater - Some Song (X Remix)",
        "Repeater Some Song X Remix", "Repeater Some Song", "Repeater Remixes Some Song X Remix",
    )})

    result = download_missing_tracks(client, MagicMock(), "Repeater", "Repeater Remixes", [row],
                                     dry_run=True, search_scope=scope)

    assert result["matched_count"] == 0


def test_another_artists_release_named_before_a_split_release_title_is_refused():
    row = {"title": "Intro", "artist": "Rotator", "track_number": "3", "duration": 60.0,
           "status": "missing", "mb_track_id": "t3"}
    path = r"Music\Somebody\Somebody - Greatest Hits - Volume 2 - 03 - Intro.mp3"
    answers = {query: [("peer", path, None)] for query in (
        "Rotator Greatest Hits Volume 2", "Greatest Hits Volume 2", "Rotator Intro", "Rotator - Intro",
        "Greatest Hits Volume 2 Intro",
    )}

    result = download_missing_tracks(client_answering(answers), MagicMock(), "Rotator", "Greatest Hits: Volume 2",
                                     [row], dry_run=True, search_scope="track")

    assert result["matched_count"] == 0


def test_on_a_one_track_release_the_healthier_better_copy_wins_over_a_number_01():
    # Any '01' agrees with a single's only position: it is no evidence outside the single's folder.
    numbered = {"username": "BullshitPancake", "hasFreeUploadSlot": True, "queueLength": 29, "uploadSpeed": 550187,
                "files": [{"filename": r"@@t\Fucked Up EDM\You Want Some Chaos EP # 12\01 Rotator - HxC mILITIA.mp3",
                           "size": 5_500_000, "bitRate": 128, "length": 339}]}
    side = {"username": "Danner1983", "hasFreeUploadSlot": True, "queueLength": 0, "uploadSpeed": 1_590_000,
            "files": [{"filename": r"@@d\Underground\tni-012 You Want Some Chaos\A1 - Rotator - HxC Militia.mp3",
                       "size": 9_000_000, "bitRate": 218, "length": 340}]}
    client = MagicMock()
    client.get_downloads.return_value = []
    client.search.side_effect = lambda query=None, **kwargs: {"responses": [numbered, side]}
    client.batch_search.side_effect = lambda queries, **kwargs: {query: {"responses": [numbered, side]} for query in queries}
    row = {"title": "HXC Militia", "artist": "Rotator", "disc_number": 1, "track_number": "1", "duration": 339.7,
           "mb_track_id": "hxc", "status": "missing"}

    result = download_missing_tracks(client, MagicMock(), "Rotator", "HXC Militia", [row], dry_run=True)

    assert [file["user"] for file in result["queued_files"]] == ["Danner1983"]
