"""Artist mode assigns each peer file to one song and needs artist evidence outside the title."""

import pytest

from discogseek.services.candidates import (
    CandidateDir,
    PeerCandidateIndex,
    evaluate_directory,
    find_best_track_candidate,
    find_track_candidates,
    path_names_artist,
    pre_parse_expected_tracks,
    release_context,
)
from tests.helpers.artist_mode import (
    FakeArtistClient,
    add_dir,
    build_index,
    raw_catalog,
    release,
    scraper_for,
)


def directory(path, names, user="peer"):
    files = [{"filename": f"{path}\\{name}", "size": 1000} for name in names]
    return CandidateDir(user, path, {"matched_search_files": files, "queue": 0, "has_slot": True})


def assignments(candidate, titles, aliases=frozenset({"target artist"}), release_title="Album"):
    expected = [{"title": title} for title in titles]
    result = evaluate_directory(candidate, pre_parse_expected_tracks(expected), expected, release_title, set(aliases))
    return result, {match["expected"]: match["matched_file"] for match in result["matched_tracks"]}


@pytest.mark.parametrize("names", [
    ["01 Paper Moon Song.flac", "02 Satellite.flac", "03 Paper Moon.flac"],
    ["03 Paper Moon.flac", "02 Satellite.flac", "01 Paper Moon Song.flac"],
])
def test_shorter_title_never_takes_the_longer_titles_file(names):
    result, chosen = assignments(
        directory("Music\\Target Artist\\Album", names), ["Paper Moon Song", "Satellite", "Paper Moon"],
    )

    assert chosen == {
        "Paper Moon Song": "01 Paper Moon Song.flac",
        "Satellite": "02 Satellite.flac",
        "Paper Moon": "03 Paper Moon.flac",
    }
    assert len({match["full_filename"] for match in result["matched_tracks"]}) == 3


def test_version_title_keeps_its_own_file():
    names = ["02 My Red Hot Car (Girl).flac", "01 My Red Hot Car.flac"]
    _, chosen = assignments(directory("Music\\Target Artist\\Album", names), ["My Red Hot Car", "My Red Hot Car (Girl)"])

    assert chosen == {"My Red Hot Car": "01 My Red Hot Car.flac", "My Red Hot Car (Girl)": "02 My Red Hot Car (Girl).flac"}


def test_word_inside_a_longer_title_does_not_count_as_a_match():
    result, chosen = assignments(
        directory("Music\\Target Artist\\Album", ["01 - Rainbow.flac", "02 - Home.flac"]),
        ["Rain", "Rainbow", "Home", "Homecoming"],
    )

    assert chosen == {"Rainbow": "01 - Rainbow.flac", "Home": "02 - Home.flac"}
    assert result["match_ratio"] == 0.5
    assert result["unmatched_expected"] == ["Rain", "Homecoming"]


def test_track_lookup_rejects_a_title_that_is_only_part_of_a_word():
    index = PeerCandidateIndex({("peer", "Music\\Artist\\Album"): {
        "matched_search_files": [{"filename": "Music\\Artist\\Album\\03 - Train.flac"}],
    }})

    assert find_best_track_candidate(index, "Rain", {"Artist"}) is None


def test_untitled_tracks_are_told_apart_by_track_number():
    titles = ["[untitled]"] * 4
    client = FakeArtistClient()
    scraper = scraper_for(raw_catalog([release("Untitled Works", titles)]), client)
    folder = "Music\\Target Artist\\Untitled Works"
    files = add_dir(scraper, "peer", folder, [f"{number:02d} [untitled]" for number in (4, 2, 3, 1)])
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert sorted(client.enqueued_names()) == sorted(file["filename"] for file in files)
    assert scraper.verified_releases[0]["matched_count"] == 4


def test_browsed_directory_replaces_its_search_hits_in_every_lookup():
    folder = "Music\\Target Artist\\Night Signals"
    index = PeerCandidateIndex({("peer", folder): {
        "matched_search_files": [{"filename": f"{folder}\\01 Neon Memory.flac"}],
        "full_directory_files": None,
    }})
    expected = [{"title": "Neon Memory"}, {"title": "Morning Light"}]

    index.update_directory("peer", folder, {"full_directory_files": [
        {"filename": f"{folder}\\01 Neon Memory.flac"}, {"filename": f"{folder}\\02 Morning Light.flac"},
    ]})

    directories = index.get_candidate_dirs_for_release("Night Signals", pre_parse_expected_tracks(expected), 2)
    assert [len(candidate.audio_files) for candidate in directories] == [2]
    assert [candidate.full_filename for candidate in find_track_candidates(index, "Morning Light", {"target artist"})] == [
        f"{folder}\\02 Morning Light.flac",
    ]
    assert len(index.all_dirs) == 1
    assert len(index.all_audio_files) == 2


@pytest.mark.parametrize("track,path", [
    ("Cobra", "@@r\\SOUND LIBRARIES\\TRAP\\Cymatics - Cobra\\Crashes\\Cymatics - Cobra Crash 1.wav"),
    ("Cobra", "@@g\\Music\\Limp Bizkit\\Gold Cobra\\03 Cobra.mp3"),
    ("Dream Portal", "@@d\\Music\\Ronan\\Dream Portal EP\\01 Dream Portal.mp3"),
    ("Hip Hop Mix", "@@m\\mixes\\DJ-Vadim---Live-in-Leeds-Hip-Hop-mix.mp3"),
    ("Afterglow", "@@r\\Music\\Third Band\\Live\\07 Eternal Afterglow.flac"),
])
def test_standalone_recording_is_not_taken_from_another_artists_file(track, path):
    client = FakeArtistClient()
    scraper = scraper_for(raw_catalog([], recordings=[{"id": "rec-single", "title": track}],
                                      aliases=["Matt Cutler"]), client)
    folder, name = path.rsplit("\\", 1)
    add_dir(scraper, "someone", folder, [name])
    build_index(scraper)

    scraper._reconcile_standalone_tracks()
    scraper._queue_downloads()

    assert scraper.verified_standalone_tracks == []
    assert [item["title"] for item in scraper.unresolved_standalone_tracks] == [track]
    assert client.calls == []


@pytest.mark.parametrize("path", [
    "@@a\\Music\\Target Artist\\Cobra\\01 Cobra.flac",
    "@@b\\stuff\\Target Artist - Cobra.mp3",
])
def test_standalone_recording_is_taken_from_the_artists_own_file(path):
    client = FakeArtistClient()
    scraper = scraper_for(raw_catalog([], recordings=[{"id": "rec-single", "title": "Cobra"}]), client)
    folder, name = path.rsplit("\\", 1)
    add_dir(scraper, "someone", folder, [name])
    add_dir(scraper, "other", "@@r\\SOUND LIBRARIES\\Cymatics - Cobra", ["Cymatics - Cobra Crash 1.wav"])
    build_index(scraper)

    scraper._reconcile_standalone_tracks()
    scraper._queue_downloads()

    assert client.enqueued_names() == [path]


def test_single_prefers_the_artists_own_folder_over_another_artists_lossless_copy():
    client = FakeArtistClient()
    scraper = scraper_for(raw_catalog([release("Afterglow", ["Afterglow"], primary_type="Single")]), client)
    theirs = add_dir(scraper, "theirs", "@@t\\Music\\Some Other Band - Afterglow [WEB FLAC]", ["01 Afterglow"])
    ours = add_dir(scraper, "ours", "@@o\\Music\\Target Artist\\Afterglow (Single)", ["01 Afterglow"], "mp3")
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert client.enqueued_names() == [ours[0]["filename"]]
    assert theirs[0]["filename"] not in client.enqueued_names()


def test_single_named_like_its_track_needs_the_artists_name_somewhere():
    client = FakeArtistClient()
    scraper = scraper_for(raw_catalog([release("Afterglow", ["Afterglow"], primary_type="Single")]), client)
    add_dir(scraper, "anyone", "@@x\\Music\\Afterglow", ["01 Afterglow"])
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert client.calls == []
    assert [entry["title"] for entry in scraper.unresolved_tracks] == ["Afterglow"]


def test_loose_file_of_another_remix_is_not_taken_for_a_named_version():
    client = FakeArtistClient()
    scraper = scraper_for(raw_catalog([release("GUNSHOTTA (VIP)", ["GUNSHOTTA (VIP)"], primary_type="Single")]), client)
    add_dir(scraper, "remixes", "@@b\\Music\\Target Artist\\Vapor City Remixes", ["Gunshotta (Om Unit's Rollers VIP)"])
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert client.calls == []
    assert [entry["title"] for entry in scraper.unresolved_tracks] == ["GUNSHOTTA (VIP)"]


@pytest.mark.parametrize("folder,accepted", [
    ("@@b\\Music\\Target Artist\\Vapor City", False),
    ("@@b\\Music\\Target Artist\\Floss (2002)", True),
])
def test_title_shared_by_two_songs_needs_the_releases_own_folder(folder, accepted):
    client = FakeArtistClient()
    scraper = scraper_for(raw_catalog([
        release("Floss", ["Floss"], 0, primary_type="Single"),
        release("Urban Biology", ["Cream Soda", "Floss"], 1),
    ]), client)
    files = add_dir(scraper, "peer", folder, ["11 Floss"])
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert client.calls == ([("peer", files)] if accepted else [])


@pytest.mark.parametrize("folder,credits", [
    ("Lone", True),
    ("Lone (UK electronic artist Matt Cutler)", True),
    ("[NONE] Lone - The Light Year Wave Tape (2020)", True),
    ("2014 - Lone - Reality Testing", True),
    ("Lone-Echolocations_EP-(RS1103)-Vinyl-READNFO-2011-BNP", True),
    ("BIKOKO_x_Lone-Miracle_Mile-(3618024616939)-16BIT-WEB-FLAC-2026-DASH", True),
    ("Lone_Always Inside Your Head (Expanded) (Greco-Roman)", True),
    ("Lone 2011 - Echolocations EP", True),
    ("Lone Wolf - Cobra (2010)", False),
    ("Lone Wolf", False),
    ("Lone_Wolf-Cobra-WEB-2010-GRP", False),
    ("K-Lone - 2023 - Swells", False),
    ("The New Adventures of the Lone Ranger", False),
    ("Lone Sentinel", False),
])
def test_only_a_folder_credited_to_the_artist_is_artist_evidence(folder, credits):
    assert path_names_artist(f"@@p\\Music\\{folder}", {"lone", "matt cutler"}) is credits


def test_a_folder_of_another_artist_whose_name_contains_the_artists_name_is_not_used():
    client = FakeArtistClient()
    raw = raw_catalog([release("Cobra", ["Cobra"], primary_type="Single")], aliases=["Lone"])
    scraper = scraper_for(raw, client)
    add_dir(scraper, "wolf", "@@w\\Music\\Lone Wolf - Cobra (2010)", ["01 Cobra"])
    add_dir(scraper, "klone", "@@k\\Music\\K-Lone\\Cobra", ["01 Cobra"])
    ours = add_dir(scraper, "ours", "@@o\\Music\\Lone\\Singles", ["Lone - Cobra.mp3"])
    build_index(scraper)

    scraper._reconcile_primary_releases()
    scraper._queue_downloads()

    assert client.enqueued_names() == [ours[0]["filename"]]


def test_standalone_recording_is_not_taken_from_an_artist_named_like_a_longer_name():
    client = FakeArtistClient()
    scraper = scraper_for(raw_catalog([], recordings=[{"id": "rec-single", "title": "Cobra"}], aliases=["Lone"]), client)
    add_dir(scraper, "ranger", "@@r\\Music\\The Lone Ranger\\Soundtrack", ["05 Cobra"])
    build_index(scraper)

    scraper._reconcile_standalone_tracks()

    assert scraper.verified_standalone_tracks == []


@pytest.mark.parametrize("path,release_title,expected", [
    ("Music\\FLAC\\Lone\\Ambivert Tools Volume One", "Ambivert Tools, Volume One", True),
    ("Music\\FLAC\\Lone\\Ambivert Tools Vol One", "Ambivert Tools, Volume One", True),
    ("Music\\FLAC\\Lone\\Ambivert Tools Volume Two", "Ambivert Tools, Volume One", False),
])
def test_edition_words_do_not_hide_the_release_title_in_a_folder(path, release_title, expected):
    assert release_context(path, release_title) is expected
