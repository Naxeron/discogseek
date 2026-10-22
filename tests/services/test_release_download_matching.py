"""Which peer files the release downloader accepts for a missing row: positions, artists and titles."""

import re
from unittest.mock import MagicMock

import pytest

from discogseek.services.library_download import download_missing_tracks


def peer_file(entry):
    return dict(entry) if isinstance(entry, dict) else {"filename": entry, "size": 1000}


def fake_client(files, only=None, user="peer", meta=None):
    """A client whose searches return one peer's files, for every query or for queries `only` accepts."""
    client = MagicMock()
    client.get_downloads.return_value = []
    client.browse_directory.return_value = []
    response = {"responses": [{"username": user, "files": [peer_file(entry) for entry in files], **(meta or {})}]}

    def answer(query):
        return response if only is None or only(query) else {"responses": []}

    client.search.side_effect = lambda query, **kwargs: answer(query)
    client.batch_search.side_effect = lambda queries, **kwargs: {query: answer(query) for query in queries}
    return client


def scope_queries(scope, track_title, release):
    """Release scope: only album queries answer. Track scope: only the row's track queries answer."""
    # Queries are searched without punctuation: 'B2 (tellur remix)' as 'B2 tellur remix'.
    title_words = re.findall(r"\w+", track_title.casefold())
    if scope == "track":
        return lambda query: all(word in query.casefold() for word in title_words)
    if all(word in release.casefold() for word in title_words):
        return None  # A title track's queries are the album's.
    return lambda query: not all(word in query.casefold() for word in title_words)


def matched(artist, release, track, files, scope, **kwargs):
    """Rows matched when only the given scope's searches return the files."""
    tracks = track if isinstance(track, list) else [track]
    client = fake_client(files if isinstance(files, list) else [files],
                         only=scope_queries(scope, tracks[0]["title"], release))
    result = download_missing_tracks(
        client, MagicMock(), artist, release, tracks, dry_run=True,
        search_scope=scope, **kwargs,
    )
    return result


def matched_files(artist, release, track, files, scope, **kwargs):
    return [file["filename"] for file in matched(artist, release, track, files, scope, **kwargs)["queued_files"]]


# candidate-matching-3: the same recording on a single or compilation keeps its own numbering.
@pytest.mark.parametrize("filename", [
    r"Music\Lone - Cali Drought (Single)\01 - Cali Drought.flac",
    r"Music\VA - DJ-Kicks Lone\07 - Lone - Cali Drought.flac",
])
def test_track_scope_accepts_recording_numbered_by_another_release(filename):
    track = {"title": "Cali Drought", "artist": "Lone", "track_number": 4}

    assert matched_files("Lone", "Emerald Fantasy Tracks", track, filename, "track") == [filename]


@pytest.mark.parametrize("scope", ["release", "track"])
def test_release_folder_numbering_conflict_is_still_rejected(scope):
    track = {"title": "Pulsewidth", "artist": "Aphex Twin", "track_number": 3}

    assert matched_files("Aphex Twin", "Album", track, r"Aphex Twin\Album\04 Pulsewidth.flac", scope) == []


@pytest.mark.parametrize("scope", ["release", "track"])
@pytest.mark.parametrize("filename", [
    r"Artist\Drukqs\05 Introduction.flac",
    r"Artist\Drukqs\Introduction.flac",
    r"Artist\Drukqs\01 Introduction.flac",
])
def test_repeated_title_is_not_taken_from_another_release(scope, filename):
    tracks = [
        {"title": "Introduction", "artist": "Artist", "disc_number": 1, "track_number": 1},
        {"title": "Introduction", "artist": "Artist", "disc_number": 2, "track_number": 1},
    ]

    assert matched_files("Artist", "Selected Ambient Works", tracks, filename, scope) == []


# candidate-matching-4 and release-download-6: catalog numbers and years are not disc numbers.
@pytest.mark.parametrize("scope", ["release", "track"])
@pytest.mark.parametrize("artist, release, track, filename", [
    ("Machinedrum", "Vapor City Archives", {"title": "Boxoff", "track_number": 3},
     r"Music\Machine Drum\[ZEN CD 215] Machinedrum - Vapor City Archives\03 - Boxoff.mp3"),
    ("Machine Drum", "Half the Battle", {"title": "Half the Battle", "track_number": 1},
     r"music\[MERCK 011] Machine_Drum-Half_the_Battle-CD-2002-PULSE\01 - Half the Battle.mp3"),
    ("Pivot", "O Soundtrack My Heart", {"title": "Sweet Crude", "track_number": 3},
     r"music\Pivot\[WARP CD 166] Pivot - O Soundtrack My Heart - 2008\03 - Sweet Crude.flac"),
    ("Lone", "Galaxy Garden", {"title": "Spirals", "track_number": 12},
     r"music\Lone\Galaxy Garden (CD 2012)\12 - Spirals.flac"),
])
def test_catalog_and_year_folders_carry_no_disc(scope, artist, release, track, filename):
    assert matched_files(artist, release, track, filename, scope) == [filename]


@pytest.mark.parametrize("scope", ["release", "track"])
def test_disc_folder_still_conflicts(scope):
    track = {"title": "Pulsewidth", "artist": "Artist", "disc_number": 1, "track_number": 3}

    assert matched_files("Artist", "Album", track, r"Artist\Album\CD 2\03 Pulsewidth.flac", scope) == []


@pytest.mark.parametrize("scope", ["release", "track"])
@pytest.mark.parametrize("artist, release, track, filename", [
    ("M83", "Hurry Up, We're Dreaming", {"title": "Midnight City", "track_number": 2},
     r"music\M83 - Hurry Up\M83 - Midnight City.flac"),
    ("U2", "Achtung Baby", {"title": "One", "track_number": 3}, r"music\U2 - One.flac"),
])
def test_letter_digit_artist_prefix_is_not_a_vinyl_side(scope, artist, release, track, filename):
    assert matched_files(artist, release, track, filename, scope) == [filename]


# candidate-matching-6, release-download-3, release-download-A1: credited names, not credit strings.
@pytest.mark.parametrize("scope", ["release", "track"])
@pytest.mark.parametrize("credit", ["Lone feat. Bikôkô", "Lone"])
@pytest.mark.parametrize("filename", [
    r"music\Lone\Hyperphantasia\05 - Lone - Miracle Mile.flac",
    r"music\Lone\Hyperphantasia\Lone - Hyperphantasia - 05 - Miracle Mile.flac",
    r"music\Lone\Hyperphantasia\05. Lone, Bikoko - Miracle Mile.flac",
    r"music\Lone\Hyperphantasia\05 Lone feat. Bikôkô - Miracle Mile.flac",
])
def test_featured_and_joint_credits_match_peer_prefixes(scope, credit, filename):
    track = {"title": "Miracle Mile", "artist": credit, "track_number": 5}

    assert matched_files("Lone", "Hyperphantasia", track, filename, scope) == [filename]


@pytest.mark.parametrize("scope", ["release", "track"])
def test_other_artist_prefix_is_rejected(scope):
    track = {"title": "Miracle Mile", "artist": "Lone feat. Bikôkô", "track_number": 5}

    assert matched_files("Lone", "Hyperphantasia", track,
                         r"music\Lone\Hyperphantasia\05 - Bonobo - Miracle Mile.flac", scope) == []


@pytest.mark.parametrize("scope", ["release", "track"])
@pytest.mark.parametrize("artist, release, track, filename", [
    ("Lone", "Emerald Fantasy Tracks", {"title": "Big World", "artist": "Lone feat. Lou Hayter", "track_number": 5},
     r"music\Lone - Emerald Fantasy Tracks\05 - Lone - Big World.flac"),
    ("Lone", "Emerald Fantasy Tracks", {"title": "Big World", "artist": "Lone feat. Lou Hayter", "track_number": 5},
     r"music\Lone - Emerald Fantasy Tracks\05 Lone - Big World (feat. Lou Hayter).flac"),
    ("Machinedrum", "Psyconia", {"title": "Only One", "artist": "Machinedrum", "track_number": 1},
     r"music\Machinedrum - Psyconia\01 Machinedrum feat. Angelica Bess - Only One.flac"),
    ("The Prodigy", "The Fat of the Land", {"title": "Firestarter", "artist": "The Prodigy", "track_number": 4},
     r"music\Prodigy - The Fat of the Land\04 - Prodigy - Firestarter.flac"),
    ("Various Artists", "Bleep Showcase, Volume 1",
     {"title": "Polaris", "artist": "The Yellow Moon Band", "track_number": 10},
     r"music\Bleep Showcase Volume 1\10 - Yellow Moon Band - Polaris.mp3"),
    ("Machine Drum", "Bidnezz",
     {"title": "Wallis & Futuna (Machine Drum remix)", "artist": "Steve & Rob", "track_number": 13},
     r"music\Machinedrum\Bidnezz\Machinedrum - Bidnezz - 13 - Wallis & Futuna (Machine Drum remix).flac"),
    ("Various Artists", "Modeselektion Vol.04 #2",
     {"title": "Peace Dub", "artist": "Sarah Farina & Yo van Lenz", "track_number": 4},
     r"Rac\Modeselektor\Modeselektion Vol.04 #2 (2018)\Modeselektor - Modeselektion Vol.04 #2 - 04 - Peace Dub.mp3"),
])
def test_credit_variants_and_album_prefixes_are_accepted(scope, artist, release, track, filename):
    assert matched_files(artist, release, track, filename, scope) == [filename]


@pytest.mark.parametrize("scope", ["release", "track"])
def test_another_artist_on_a_compilation_is_rejected(scope):
    track = {"title": "Polaris", "artist": "The Yellow Moon Band", "track_number": 10}

    assert matched_files("Various Artists", "Bleep Showcase, Volume 1", track,
                         r"music\Bleep Showcase Volume 1\10 - Bonobo - Polaris.mp3", scope) == []


# release-download-4 and verification-audit-A2: flat and disc-prefixed numbering of multi-disc releases.
@pytest.mark.parametrize("scope", ["release", "track"])
@pytest.mark.parametrize("disc, number, filename", [
    (2, 1, r"music\Lone\Galaxy Garden (Deluxe)\201 - Spirals.flac"),
    (1, 3, r"music\Lone\Galaxy Garden (Deluxe)\103 - Spirals.flac"),
])
def test_disc_track_prefixes_match(scope, disc, number, filename):
    track = {"title": "Spirals", "artist": "Lone", "disc_number": disc, "track_number": number}

    assert matched_files("Lone", "Galaxy Garden", track, filename, scope) == [filename]


def official_media(counts, titles=None):
    rows = []
    for disc, count in enumerate(counts, 1):
        for number in range(1, count + 1):
            rows.append({"title": f"Song {disc}-{number}", "artist": "Lone", "disc_number": disc,
                         "track_number": str(number)})
    for (disc, number), title in (titles or {}).items():
        next(row for row in rows if row["disc_number"] == disc and row["track_number"] == str(number))["title"] = title
    return rows


@pytest.mark.parametrize("scope", ["release", "track"])
def test_flat_numbering_uses_official_media(scope):
    official = official_media([12, 12], {(2, 1): "Spirals"})
    track = official[12]
    folder = r"music\Lone\Galaxy Garden (Deluxe)" + "\\"

    assert matched_files("Lone", "Galaxy Garden", track, folder + "13 - Lone - Spirals.flac", scope,
                         official_tracks=official) == [folder + "13 - Lone - Spirals.flac"]
    assert matched_files("Lone", "Galaxy Garden", track, folder + "14 - Lone - Spirals.flac", scope,
                         official_tracks=official) == []


@pytest.mark.parametrize("scope", ["release", "track"])
def test_flat_numbering_uses_absolute_position_of_the_row(scope):
    track = {"title": "Listen 2 Me", "artist": "Machinedrum", "disc_number": 2, "track_number": "7",
             "absolute_position": 18}
    folder = r"Music\Machinedrum\Machinedrum - Room(s) Extended - 2012" + "\\"

    assert matched_files("Machinedrum", "Room(s) Extended", track, folder + "18 Listen 2 Me.flac", scope) == [
        folder + "18 Listen 2 Me.flac"]
    assert matched_files("Machinedrum", "Room(s) Extended", track, folder + "19 Listen 2 Me.flac", scope) == []


@pytest.mark.parametrize("scope", ["release", "track"])
def test_artist_prefixed_names_are_position_checked(scope):
    track = {"title": "Ladle", "artist": "Machine Drum", "disc_number": 1, "track_number": 3}

    assert matched_files("Machine Drum", "Bidnezz", track,
                         r"music\Machinedrum\Bidnezz\Machinedrum - Bidnezz - 21 - Ladle.flac", scope) == []


@pytest.mark.parametrize("scope", ["release", "track"])
def test_other_edition_numbering_is_accepted_with_matching_duration(scope):
    """'Now You Know' 2x12" side 2 #1 'Monique' is track 8 of the CD edition peers share."""
    track = {"title": "Monique", "artist": "Machine Drum", "disc_number": 2, "track_number": "1", "duration": 67.4}
    folder = r"music\m\Machinedrum - Now You Know" + "\\"
    same = {"filename": folder + "08 Machine Drum - Monique.flac", "size": 1000, "length": 67}
    different = {"filename": folder + "08 Machine Drum - Monique.flac", "size": 1000, "length": 302}

    assert matched_files("Machine Drum", "Now You Know", track, [same], scope) == [same["filename"]]
    assert matched_files("Machine Drum", "Now You Know", track, [different], scope) == []


# release-download-5, verification-audit-A3: vinyl side positions.
def cubist_reggae():
    return [
        {"title": "Cubist Reggae", "artist": "Venetian Snares", "disc_number": 1, "track_number": "A1"},
        {"title": "Sanic Spheres", "artist": "Venetian Snares", "disc_number": 1, "track_number": "A2"},
        {"title": "The Identification Circles Levitate", "artist": "Venetian Snares", "disc_number": 1,
         "track_number": "B1"},
        {"title": "Paul's Dreams", "artist": "Venetian Snares", "disc_number": 1, "track_number": "B2"},
    ]


@pytest.mark.parametrize("scope", ["release", "track"])
@pytest.mark.parametrize("basename", [
    "03 - The Identification Circles Levitate.flac",
    "B1 - The Identification Circles Levitate.flac",
])
def test_vinyl_side_matches_side_token_and_sequence(scope, basename):
    official = cubist_reggae()
    filename = "music\\Venetian Snares\\Cubist Reggae\\" + basename

    assert matched_files("Venetian Snares", "Cubist Reggae", official[2], filename, scope,
                         official_tracks=official) == [filename]


@pytest.mark.parametrize("scope", ["release", "track"])
@pytest.mark.parametrize("basename", [
    "05 - The Identification Circles Levitate.flac",
    "B2 - The Identification Circles Levitate.flac",
])
def test_vinyl_side_rejects_other_positions(scope, basename):
    official = cubist_reggae()
    filename = "music\\Venetian Snares\\Cubist Reggae\\" + basename

    assert matched_files("Venetian Snares", "Cubist Reggae", official[2], filename, scope,
                         official_tracks=official) == []


@pytest.mark.parametrize("scope", ["release", "track"])
@pytest.mark.parametrize("basename", [
    "13 - Wallis & Futuna (Machine Drum Remix).flac",
    "C1 - Wallis & Futuna (Machine Drum remix).flac",
])
def test_second_lp_side_uses_release_wide_index(scope, basename):
    official = [{"title": f"Song {number}", "artist": "Machine Drum", "disc_number": 1, "track_number": str(number)}
                for number in range(1, 13)]
    official += [
        {"title": "Wallis & Futuna (Machine Drum remix)", "artist": "Steve & Rob", "disc_number": 2,
         "track_number": "C1"},
        {"title": "Dog Actually (Machine Drum remix)", "artist": "Mic Mell", "disc_number": 2, "track_number": "C2"},
    ]
    filename = "music\\Machinedrum\\Bidnezz\\" + basename

    assert matched_files("Machine Drum", "Bidnezz", official[12], filename, scope,
                         official_tracks=official) == [filename]


@pytest.mark.parametrize("scope", ["release", "track"])
@pytest.mark.parametrize("artist, track, basename", [
    ("Venetian Snares", {"title": "Perpetual Darkness", "artist": "Venetian Snares", "track_number": "B1"},
     "B1 - Perpetual Darkness.flac"),
    ("Various Artists", {"title": "Perpetual Darkness", "artist": "Bombardier", "track_number": "B1"},
     "B1. Bombardier - Perpetual Darkness.flac"),
    ("Various Artists", {"title": "Flesh Wound", "artist": "Bombardier", "track_number": "A2"},
     "A2 Bombardier - Flesh Wound.flac"),
])
def test_vinyl_side_prefix_is_not_an_artist(scope, artist, track, basename):
    filename = "music\\Crippled Children\\" + basename

    assert matched_files(artist, "Crippled Children", track, filename, scope) == [filename]


def test_vinyl_rows_keep_their_own_medium_and_side():
    """B1 and C1 of a double LP share a title; a single LP's B2 stays on medium 1."""
    rows = [
        {"title": "Interlude", "artist": "Artist", "disc_number": 1, "track_number": "B1", "position_token": "B1"},
        {"title": "Interlude", "artist": "Artist", "disc_number": 2, "track_number": "C1", "position_token": "C1"},
        {"title": "Closer", "artist": "Artist", "disc_number": 1, "track_number": "B2", "position_token": "B2"},
    ]
    folder = "Music\\Artist\\Artist - Double Album\\"
    files = [folder + "B1 - Interlude.flac", folder + "C1 - Interlude.flac", folder + "B2 - Closer.flac"]

    result = download_missing_tracks(fake_client(files), MagicMock(), "Artist", "Double Album",
                                     [dict(row) for row in rows], official_tracks=rows, dry_run=True)

    assert sorted(row["track_number"] for row in result["matched_tracks"]) == ["B1", "B2", "C1"]
    assert sorted((file["disc_number"], file["track_number"], file["filename"]) for file in result["queued_files"]) == [
        (1, "B1", files[0]), (1, "B2", files[2]), (2, "C1", files[1]),
    ]


@pytest.mark.parametrize("scope", ["release", "track"])
def test_numbered_other_artist_prefix_is_rejected(scope):
    track = {"title": "Perpetual Darkness", "artist": "Venetian Snares", "track_number": "B1"}

    assert matched_files("Venetian Snares", "Crippled Children", track,
                         r"music\Crippled Children\01 Someone Else - Perpetual Darkness.flac", scope) == []


# release-download-7: noise in peer filenames.
@pytest.mark.parametrize("scope", ["release", "track"])
@pytest.mark.parametrize("title, number, basename", [
    ("YASTM.m4a", 7, "07 - YASTM.m4a.mp3"),
    ("Cody", 7, "07 - Cody (2009).mp3"),
    ("Cody", 7, "07 - Cody [FLAC] [889 kbps].flac"),
    ("Cody", 7, "07 - Cody (Explicit).flac"),
    ("Cody", 7, "07 - Cody.aif"),
    ("Entrau", 1, "1-01_entrau.flac"),
    ("Entrau", 1, "01-machine_drum--entrau-sb.mp3"),
])
def test_decorated_filenames_match(scope, title, number, basename):
    track = {"title": title, "artist": "Machine Drum", "track_number": number}
    filename = "music\\Machine Drum\\Album\\" + basename

    assert matched_files("Machine Drum", "Album", track, filename, scope) == [filename]


@pytest.mark.parametrize("scope", ["release", "track"])
def test_dated_remix_is_another_version(scope):
    track = {"title": "Cody", "artist": "Lone", "track_number": 7}

    assert matched_files("Lone", "Album", track, r"music\Lone\Album\07 - Cody (2009 Remix).flac", scope) == []


# Bracketed position tokens after label and date noise.
@pytest.mark.parametrize("scope", ["release", "track"])
def test_bracketed_track_numbers_after_label_prefix(scope):
    track = {"title": "Mama4", "artist": "Ay Fast", "track_number": 2}
    folder = "@@qzqgu\\!LABELS\\!Schematic\\[SCH060] VA - StiLL iLL\\"
    right = folder + "VA (StiLL iLL)-[SCH 060]-2010.09.28-[02]-Ay Fast - Mama4.mp3"
    wrong = folder + "VA (StiLL iLL)-[SCH 060]-2010.09.28-[03]-Ay Fast - Mama4.mp3"

    assert matched_files("Various Artists", "StiLL iLL", track, right, scope) == [right]
    assert matched_files("Various Artists", "StiLL iLL", track, wrong, scope) == []


# browser-queue-A1: a title that is only a descriptor.
@pytest.mark.parametrize("scope", ["release", "track"])
def test_descriptor_only_titles_match_exact_peer_file(scope):
    track = {"title": "B2 (tellur remix)", "artist": "Aki", "track_number": 2}
    folder = "Music\\Aki\\Temporarily Fixed Consciousness\\"

    assert matched_files("Aki", "Temporarily Fixed Consciousness", track,
                         folder + "02 - B2 (tellur remix).flac", scope) == [folder + "02 - B2 (tellur remix).flac"]
    assert matched_files("Aki", "Temporarily Fixed Consciousness", track,
                         folder + "02 - B1 (tellur remix).flac", scope) == []


# operational-evidence-7: an exact title alone does not identify the recording.
def test_track_scope_needs_artist_or_duration_evidence():
    track = {"title": "Playhouse", "artist": "Azealia Banks & Lone", "track_number": 1, "duration": 215.0}
    files = [r"music\Sabrina_spotify\Pretty Ricky\01 - Playhouse.flac"]

    assert matched_files("Azealia Banks & Lone", "Playhouse", track, files, "track") == []
    timed = {"filename": r"music\Sabrina_spotify\Unsorted\01 - Playhouse.flac", "size": 1000, "length": 216}
    assert matched_files("Azealia Banks & Lone", "Playhouse", track, [timed], "track") == [timed["filename"]]


@pytest.mark.parametrize("filename", [
    r"Music\Azealia Banks\Playhouse\01 - Playhouse.flac",
    r"Music\Lone\Playhouse\01 Playhouse.flac",
])
def test_track_scope_accepts_a_credited_artist_folder(filename):
    track = {"title": "Playhouse", "artist": "Azealia Banks & Lone", "track_number": 1}

    assert matched_files("Azealia Banks & Lone", "Playhouse", track, filename, "track") == [filename]


def test_title_only_album_results_need_artist_evidence():
    """release-download-8 (b): a same-titled album of another artist is never taken from the title query."""
    track = {"title": "Miracle Mile", "artist": "Lone feat. Bikôkô", "track_number": 5}
    client = fake_client([r"Other Artist\Hyperphantasia\05 - Miracle Mile.flac"],
                         only=lambda query: query == "Hyperphantasia")

    result = download_missing_tracks(client, MagicMock(), "Lone", "Hyperphantasia", [track], dry_run=True)

    assert result["matched_tracks"] == []


# artist-download-complete-verify-recording-titles: printed titles in complete-release verification.
def test_complete_release_accepts_alternative_titles():
    official = [
        {"title": "Morning Song", "alt_titles": ["Song for the Morning"], "artist": "Lone", "track_number": "1",
         "status": "missing"},
        {"title": "Evening", "artist": "Lone", "track_number": "2", "status": "found"},
    ]
    client = fake_client([
        r"music\Lone\Album\01 - Song for the Morning.flac",
        r"music\Lone\Album\02 - Evening.flac",
    ])

    result = download_missing_tracks(client, MagicMock(), "Lone", "Album", [official[0]],
                                     complete_release_tracks=official)

    assert result["complete_release_available"] is True
    assert [file["filename"] for file in result["enqueued_files"]] == [
        r"music\Lone\Album\01 - Song for the Morning.flac"]


def test_release_titled_with_stop_words_names_its_folder():
    """A release called 'Album' is that folder's release, not a format subfolder of the artist's."""
    track = {"title": "Pulsewidth", "track_number": 3}
    downloads = [{"username": "z-existing", "directories": [{"files": [
        {"id": "x", "filename": "Artist/Album/03 Pulsewidth.mp3", "state": "Completed, Succeeded"},
    ]}]}]
    client = fake_client(["Artist/Album/03 Pulsewidth.flac"], user="a-better")
    client.get_downloads.return_value = downloads

    result = download_missing_tracks(client, MagicMock(), "Artist", "Album", [track])

    assert result["queued_files"][0]["user"] == "z-existing"
    client.enqueue_download.assert_not_called()


@pytest.mark.parametrize("scope", ["release", "track"])
@pytest.mark.parametrize("artist, release, track, filename", [
    ("Jay-Z", "The Blueprint", {"title": "Takeover", "track_number": 2},
     r"mp3\Jay-Z-The_Blueprint-2001-GRP\02-jay-z-takeover.mp3"),
    ("Bong-Ra", "Grindcore Is Dead", {"title": "Mashup Core", "track_number": 3},
     r"mp3\Bong-Ra-Grindcore_Is_Dead-WEB-2010-GRP\03-bong-ra-mashup_core.mp3"),
])
def test_scene_names_keep_hyphenated_artists(scope, artist, release, track, filename):
    assert matched_files(artist, release, dict(track, artist=artist), filename, scope) == [filename]


@pytest.mark.parametrize("scope", ["release", "track"])
def test_three_digit_numbers_of_a_long_medium_are_absolute(scope):
    official = [{"title": f"Song {number}", "artist": "Various Artists", "track_number": str(number)}
                for number in range(1, 121)]
    official[104] = {"title": "Blessed", "artist": "Leon Vynehall", "track_number": "105"}
    filename = r"music\Bleep\Bleep 100 Tracks 2020\105 - Leon Vynehall - Blessed.flac"

    assert matched_files("Various Artists", "Bleep 100 Tracks 2020", official[104], filename, scope,
                         official_tracks=official) == [filename]


def test_single_word_artist_inside_another_name_is_no_evidence():
    track = {"title": "Big World", "artist": "Lone", "track_number": 5}

    assert matched_files("Lone", "Emerald Fantasy Tracks", track,
                         r"Music\Lone Wolf\The Lighthouse\01 - Big World.flac", "track") == []
    assert matched_files("Lone", "Emerald Fantasy Tracks", track,
                         r"Music\Electronic\Lone\Singles\01 - Big World.flac", "track") == [
        r"Music\Electronic\Lone\Singles\01 - Big World.flac"]


# A one-word release title names a folder only as a whole part of its name.
@pytest.mark.parametrize("scope", ["release", "track"])
@pytest.mark.parametrize("filename", [
    r"Music\Unsorted\Memory Tapes - People.flac",
    r"Music\Fond Memory - People (Single)\People.flac",
])
def test_one_word_release_title_inside_another_name_is_not_the_release(scope, filename):
    track = {"title": "People", "artist": "Lazer Sword", "track_number": 11}

    assert matched_files("Lazer Sword", "Memory", track, filename, scope) == []


@pytest.mark.parametrize("scope", ["release", "track"])
@pytest.mark.parametrize("filename", [
    r"Music\Memory (2012) [FLAC]\11 People.flac",
    r"Music\Lazer_Sword-Memory-(MONKEYTOWN025)-WEB-2012-DASH\11-people.flac",
    r"Music\Lazer Sword Memory\11 - People.flac",
])
def test_one_word_release_title_names_its_own_folder(scope, filename):
    track = {"title": "People", "artist": "Lazer Sword", "track_number": 11}

    assert matched_files("Lazer Sword", "Memory", track, filename, scope) == [filename]


@pytest.mark.parametrize("scope", ["release", "track"])
@pytest.mark.parametrize("folder, is_release", [
    (r"Music\VA Cambio 2012", True),
    (r"Music\Various Artists - Cambio", True),
    (r"Music\VA - Cambio de Piel", False),
])
def test_one_word_compilation_title_ignores_va_words(scope, folder, is_release):
    """Numbering is enforced only in the compilation's own folder."""
    track = {"title": "Friends And Lovers", "artist": "Shigeto", "track_number": 1}
    filename = folder + r"\05 Shigeto - Friends And Lovers.flac"

    assert matched_files("Various Artists", "Cambio", track, filename, scope) == ([] if is_release else [filename])


# Outside the release's folder, a clearly different duration is another recording.
@pytest.mark.parametrize("scope", ["release", "track"])
def test_duration_conflict_outside_the_release_folder_rejects_the_file(scope):
    intro = {"title": "Intro", "artist": "Lone", "track_number": 1, "duration": 40.0}
    other_album = {"filename": r"Music\Lone\Galaxy Garden\05 Intro.flac", "size": 1000, "length": 300}
    dj_mix_cut = {"title": "Airglow Fires", "artist": "Lone", "track_number": 18, "duration": 146.0}
    album_version = {"filename": r"Music\Lone\Reality Testing\06 Airglow Fires.flac", "size": 1000, "length": 361}

    assert matched_files("Lone", "Hyperphantasia", intro, other_album, scope) == []
    assert matched_files("Various Artists", "DJ-Kicks: Kamaal Williams", dj_mix_cut, album_version, scope) == []


def test_duration_conflict_in_an_unsorted_folder_with_other_release_tracks_rejects_the_file():
    tracks = [
        {"title": "Intro", "artist": "Lone", "track_number": 1, "duration": 40.0},
        {"title": "Triton", "artist": "Lone", "track_number": 6, "duration": 337.0},
        {"title": "Scattergun", "artist": "Lone", "track_number": 13, "duration": 270.0},
    ]
    folder = "Music\\Lone\\Unsorted\\"
    files = [{"filename": folder + name, "size": 1000, "length": length}
             for name, length in [("Lone - Triton.flac", 337), ("Lone - Scattergun.flac", 270),
                                  ("Lone - Intro.flac", 300)]]

    result = matched("Lone", "Hyperphantasia", tracks, files, "release")

    assert sorted(file["title"] for file in result["queued_files"]) == ["Scattergun", "Triton"]


@pytest.mark.parametrize("scope", ["release", "track"])
def test_close_duration_and_in_folder_lengths_still_match(scope):
    single = {"filename": r"Music\Lone - Cali Drought (Single)\01 - Cali Drought.flac", "size": 1000, "length": 297}
    track = {"title": "Cali Drought", "artist": "Lone", "track_number": 4, "duration": 302.0}
    # Some peers report twice the length of a VBR mp3; the release's own folder still identifies it.
    misreported = {"filename": r"Labels\Bleep\Bleep Showcase Volume 1\10. yellow moon band - polaris.mp3",
                   "size": 1000, "length": 682}
    polaris = {"title": "Polaris", "artist": "The Yellow Moon Band", "track_number": 10, "duration": 339.0}

    assert matched_files("Lone", "Emerald Fantasy Tracks", track, single, "track") == [single["filename"]]
    assert matched_files("Various Artists", "Bleep Showcase, Volume 1", polaris, misreported, scope) == [
        misreported["filename"]]
