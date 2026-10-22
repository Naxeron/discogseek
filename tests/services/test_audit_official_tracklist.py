"""Official tracklists: printed titles, every position form, MBIDs first, and honest counts."""

from unittest.mock import MagicMock

import pytest

from discogseek.services.library import LibraryReleaseService


NO_MATCH_ERROR = "MusicBrainz release matches none of the local tracks"


def official_track(number, title, recording=None, printed=None, artist=None, track_id=None, position=None):
    track = {
        "id": track_id or f"track-{recording or title}", "number": str(number),
        "recording": {"id": recording or f"rec-{title}", "title": title},
    }
    if position is not None:
        track["position"] = str(position)
    if printed is not None:
        track["title"] = printed
    if artist:
        track["artist-credit"] = [{"name": artist, "artist": {"name": artist}}]
    return track


def mb_release(media, release_id="edition", artist="Artist", title="Album", rg=None, formats=None):
    return {
        "id": release_id, "title": title,
        "artist-credit": [{"name": artist, "artist": {"name": artist}}] if artist else [],
        "release-group": rg or {"id": "group", "primary-type": "Album"},
        "medium-list": [
            {"position": str(disc), "format": (formats or {}).get(disc, "CD"), "track-list": tracks}
            for disc, tracks in enumerate(media, 1)
        ],
    }


def local_file(title, number=None, disc=1, recording=None, track_id=None, artist="Artist", token=None):
    stem = f"{number:02d} {title}" if isinstance(number, int) else (f"{number} {title}" if number else title)
    track = {
        "title": title, "artist": artist, "disc_number": disc,
        "track_number": str(number) if number is not None else None,
        "filename": f"{stem}.flac", "path": f"/music/Album/{stem}.flac", "status": "found",
        "mb_rec_ids": [recording] if recording else [], "mb_track_ids": [track_id] if track_id else [],
    }
    if token:
        track["position_token"] = token
    return track


def audit(official, tracks, artist="Artist", title="Album", release_id="edition", **local):
    mb = MagicMock()
    mb.get_release_by_id.return_value = official
    service = LibraryReleaseService(mb_client=mb, slskd_client=MagicMock())
    return service.audit_release(dict({
        "artist": artist, "title": title, "mb_release_id": release_id, "tracks": tracks,
    }, **local))


def statuses(audited):
    return [(t["disc_number"], t["track_number"], t["title"], t["status"]) for t in audited["tracks"]
            if not t.get("is_extra")]


def extras(audited):
    return [t["title"] for t in audited["tracks"] if t.get("is_extra")]


# --- Printed titles and position fields ------------------------------------------------


def test_printed_track_title_is_matched_and_recording_title_is_an_alternate():
    # Xen Cuts 3-13: printed '(Four Tet remix)', recording '(Four Tet mix)'.
    official = mb_release([[
        official_track(1, "Twice the First Time"),
        official_track(2, "Ode to the Big Sea (Four Tet mix)", printed="Ode to the Big Sea (Four Tet remix)"),
    ]])
    audited = audit(official, [
        local_file("Twice the First Time", 1),
        local_file("Ode to the Big Sea (Four Tet remix)", 2),
    ])

    assert statuses(audited) == [
        (1, "1", "Twice the First Time", "found"),
        (1, "2", "Ode to the Big Sea (Four Tet remix)", "found"),
    ]
    assert extras(audited) == []
    assert audited["tracks"][1]["alt_titles"] == ["Ode to the Big Sea (Four Tet mix)"]
    assert audited["tracks"][0]["alt_titles"] == []


def test_alternate_recording_title_still_matches_a_local_file():
    official = mb_release([[official_track(1, "東雲ブレイク", printed="Shinonome Break"),
                            official_track(2, "Kumo")]])
    audited = audit(official, [local_file("東雲ブレイク", 1), local_file("Kumo", 2)])

    assert statuses(audited) == [(1, "1", "Shinonome Break", "found"), (1, "2", "Kumo", "found")]


def test_missing_rows_carry_every_position_form():
    official = mb_release([
        [official_track("A1", "Alpha", position=1), official_track("A2", "Beta", position=2),
         official_track("B1", "Gamma", position=3)],
        [official_track("C1", "Delta", position=1), official_track("C2", "Epsilon", position=2)],
    ], formats={1: '12" Vinyl', 2: '12" Vinyl'})
    audited = audit(official, [local_file("Alpha", "A1", token="A1")])

    fields = [(t["title"], t["position_token"], t["track_position"], t["absolute_position"],
               t["medium_track_count"]) for t in audited["tracks"]]
    assert fields == [
        ("Alpha", "A1", 1, 1, 3), ("Beta", "A2", 2, 2, 3), ("Gamma", "B1", 3, 3, 3),
        ("Delta", "C1", 1, 4, 2), ("Epsilon", "C2", 2, 5, 2),
    ]
    assert [t["status"] for t in audited["tracks"]] == ["found"] + ["missing"] * 4


# --- MBIDs are applied to every official track before titles ---------------------------


def drowning_again():
    return mb_release([[
        official_track(10, "Silver Box (Daniel Avery remix)", recording="rec-10"),
        official_track(11, "Drowning Again (Christopher Ledger remix)", recording="rec-11"),
        official_track(12, "Drowning Again (Christopher Ledger 'Hypno Tool' remix)", recording="rec-12"),
    ]], artist="Danny Daze", title="::BLUE:: [Remixes]")


@pytest.mark.parametrize("tagged", [True, False], ids=["recording-tagged", "untagged-numbered"])
def test_file_identified_as_a_later_track_is_not_taken_by_an_earlier_similar_title(tagged):
    local = local_file("Drowning Again (Christopher Ledger 'Hypno Tool' remix)", 12,
                       recording="rec-12" if tagged else None, artist="Danny Daze")
    audited = audit(drowning_again(), [local], artist="Danny Daze", title="::BLUE:: [Remixes]")

    assert [t["status"] for t in audited["tracks"]] == ["missing", "missing", "found"]
    assert audited["tracks"][2]["path"] == local["path"]
    assert extras(audited) == []


def test_untagged_unnumbered_file_prefers_its_exact_full_title():
    local = local_file("Drowning Again (Christopher Ledger 'Hypno Tool' remix)", artist="Danny Daze")
    audited = audit(drowning_again(), [local], artist="Danny Daze", title="::BLUE:: [Remixes]")

    assert [t["status"] for t in audited["tracks"]] == ["missing", "missing", "found"]


def test_instrumental_mix_tagged_for_one_remixer_does_not_fill_another():
    # Refurbished Two: #10 '(Funckarma instrumental mix)' is not #6 '(Shadow Huntaz ...)'.
    official = mb_release([[
        official_track(6, "The Way It Was (Shadow Huntaz instrumental mix)", recording="rec-6",
                       artist="Strand + Tres"),
        official_track(10, "The Way It Was (Funckarma instrumental mix)", recording="rec-10",
                       artist="Strand + Tres"),
    ]], artist="Funckarma", title="Refurbished Two")
    local = local_file("The Way It Was (Funckarma instrumental mix)", 10, recording="rec-10",
                       artist="Strand + Tres")
    audited = audit(official, [local], artist="Funckarma", title="Refurbished Two")

    assert [t["status"] for t in audited["tracks"]] == ["missing", "found"]


def test_track_id_of_a_repeated_recording_names_its_own_position():
    # The same recording at #2 and #4; an unnumbered file tagged with #4's track ID.
    official = mb_release([[
        official_track(1, "One", recording="r1", track_id="t1"),
        official_track(2, "Theme", recording="theme", track_id="t2"),
        official_track(3, "Three", recording="r3", track_id="t3"),
        official_track(4, "Theme", recording="theme", track_id="t4"),
    ]])
    audited = audit(official, [local_file("Theme", None, recording="theme", track_id="t4")])

    assert [t["status"] for t in audited["tracks"]] == ["missing", "missing", "missing", "found"]


# --- Vinyl numbering --------------------------------------------------------------------


def single_vinyl():
    return mb_release([[
        official_track("A1", "Alpha", position=1), official_track("A2", "Beta", position=2),
        official_track("B1", "Gamma", position=3), official_track("B2", "Reel Cleer", position=4),
    ]], formats={1: '12" Vinyl'})


def test_sequential_local_numbers_match_vinyl_side_positions():
    tracks = [local_file(title, number) for number, title in
              enumerate(["Alpha", "Beta", "Gamma", "Reel Clear"], 1)]
    audited = audit(single_vinyl(), tracks)

    assert [t["status"] for t in audited["tracks"]] == ["found"] * 4
    assert audited["tracks"][3]["path"].endswith("04 Reel Clear.flac")
    assert extras(audited) == []
    assert (audited["found_count"], audited["missing_count"]) == (4, 0)


def test_local_side_tokens_match_vinyl_tokens_without_becoming_discs():
    # The scan reads 'B2' as disc 2 track 2; MusicBrainz lists both sides on one medium.
    tracks = [local_file(title, token, disc=1 if token.startswith("A") else 2, token=token)
              for token, title in [("A1", "Alpha"), ("A2", "Beta"), ("B1", "Gamma"), ("B2", "Reel Clear")]]
    audited = audit(single_vinyl(), tracks)

    assert [t["status"] for t in audited["tracks"]] == ["found"] * 4
    assert extras(audited) == []


def test_retitled_side_b_file_matches_by_title_on_the_only_medium():
    tracks = [local_file("Alpha", "A1", token="A1"),
              local_file("Reel Cleer", "B3", disc=2, token="B3")]
    audited = audit(single_vinyl(), tracks)

    assert [t["status"] for t in audited["tracks"]] == ["found", "missing", "missing", "found"]


def test_vinyl_position_does_not_override_a_conflicting_title():
    audited = audit(single_vinyl(), [local_file("Gamma", 1)])

    assert [t["status"] for t in audited["tracks"]] == ["missing", "missing", "found", "missing"]


def test_side_b_is_not_the_second_lp():
    # 2×12": the scan reads 'B1' as disc 2 track 1, which is not medium 2's C1. A second
    # copy of B1 (official B1 is already matched by MBID) must not fill C1.
    official = mb_release([
        [official_track("A1", "Alpha", position=1), official_track("B1", "Lullaby", position=2)],
        [official_track("C1", "Lullaby (Reprise)", position=1), official_track("D1", "Delta", position=2)],
    ], formats={1: '12" Vinyl', 2: '12" Vinyl'})
    tagged = local_file("Lullaby", "B1", disc=2, recording="rec-Lullaby", token="B1")
    copy = local_file("Lullaby", "B1", disc=2, token="B1")
    copy["path"] = "/music/Album (MP3)/B1 Lullaby.mp3"
    audited = audit(official, [tagged, copy])

    assert [status for *_, status in statuses(audited)] == ["missing", "found", "missing", "missing"]
    assert extras(audited) == ["Lullaby"]


# --- Flat numbering across discs --------------------------------------------------------


def two_by_two():
    return mb_release([
        [official_track(1, "Alpha"), official_track(2, "Beta")],
        [official_track(1, "Gamma"), official_track(2, "Delta")],
    ])


def test_flat_numbered_local_files_match_later_discs():
    tracks = [local_file(title, number) for number, title in
              enumerate(["Alpha", "Beta", "Gamma", "Delta"], 1)]
    audited = audit(two_by_two(), tracks)

    assert statuses(audited) == [
        (1, "1", "Alpha", "found"), (1, "2", "Beta", "found"),
        (2, "1", "Gamma", "found"), (2, "2", "Delta", "found"),
    ]
    assert extras(audited) == []
    assert audited["missing_count"] == 0


def test_continuous_numbering_matches_disc_two_of_a_three_disc_set():
    # Xen Cuts: 18/14/15 tracks; '19 …' is disc 2 track 1 and '33 …' disc 3 track 1.
    media = [[official_track(n, f"Disc {d} Song {n}") for n in range(1, size + 1)]
             for d, size in ((1, 18), (2, 14), (3, 15))]
    media[1][0] = official_track(1, "The Joy of X (Spotters Delight)")
    media[2][0] = official_track(1, "Twice the First Time")
    media[1][6] = official_track(7, "Original Sins")
    media[2][8] = official_track(9, "Ninja Tune (Enter the Augmenter) (The Process mix)")
    audited = audit(mb_release(media, artist="Various Artists"), [
        local_file("The Joy of X (Spotters Delight)", 19, artist="Flexus"),
        local_file("Original Sins", 25, artist="Chris Bowden"),
        local_file("Twice the First Time", 33, artist="Saul Williams"),
        # The printed subtitle is missing locally; the absolute position (41) agrees.
        local_file("Ninja Tune (The Process mix)", 41, artist="Hexstatic"),
    ], artist="Various Artists")

    found = [(t["disc_number"], t["track_number"]) for t in audited["tracks"] if t["status"] == "found"]
    assert found == [(2, "1"), (2, "7"), (3, "1"), (3, "9")]
    assert extras(audited) == []


def test_continuous_numbers_next_to_disc_prefixed_files_match_by_title():
    # The real Xen Cuts folder mixes two editions: '2-03 …' files and files numbered
    # continuously ('19 …' is disc 2 track 1 of the 18/14/15 set).
    media = [[official_track(n, f"Disc {d} Song {n}") for n in range(1, size + 1)]
             for d, size in ((1, 18), (2, 14), (3, 15))]
    media[1][0] = official_track(1, "The Joy of X (Spotters Delight)")
    media[2][0] = official_track(1, "Twice the First Time")
    tracks = [
        local_file("Disc 2 Song 3", "2-03", disc=2, artist="Neptune"),
        local_file("Disc 3 Song 2", "3-02", disc=3, artist="Bonobo"),
        local_file("The Joy of X (Spotters Delight)", 19, artist="Flexus"),
        local_file("Twice the First Time", 33, artist="Saul Williams"),
    ]
    tracks[0]["track_number"], tracks[1]["track_number"] = "3", "2"
    audited = audit(mb_release(media, artist="Various Artists"), tracks, artist="Various Artists")

    found = [(t["disc_number"], t["track_number"]) for t in audited["tracks"] if t["status"] == "found"]
    assert found == [(2, "1"), (2, "3"), (3, "1"), (3, "2")]
    assert extras(audited) == []


def test_title_without_its_subtitle_needs_the_files_own_position():
    official = mb_release([[official_track(1, "Intro"),
                            official_track(2, "Ninja Tune (Enter the Augmenter) (The Process mix)")]])
    audited = audit(official, [local_file("Ninja Tune (The Process mix)", 7)])

    assert [t["status"] for t in audited["tracks"]] == ["missing", "missing", "found"]
    assert extras(audited) == ["Ninja Tune (The Process mix)"]


def test_a_disc_one_bonus_number_is_not_the_next_disc():
    # Another edition with a 4th track on disc 1: an untitled '1-04' file is not disc 2 #1.
    official = mb_release([
        [official_track(1, "A"), official_track(2, "B"), official_track(3, "C")],
        [official_track(1, "D"), official_track(2, "E"), official_track(3, "F")],
    ])
    bonus = local_file("", 4)
    bonus.update(filename="1-04.flac", path="/music/Album/1-04.flac")
    tracks = [local_file(title, number) for number, title in enumerate(["A", "B", "C"], 1)]
    tracks += [bonus, local_file("E", 2, disc=2), local_file("F", 3, disc=2)]
    audited = audit(official, tracks)

    assert statuses(audited)[3] == (2, "1", "D", "missing")
    assert len(extras(audited)) == 1


def test_continuous_numbering_on_a_tagged_disc_matches_that_disc():
    official = mb_release([
        [official_track(1, "A"), official_track(2, "B")],
        [official_track(1, "C"), official_track(2, "D")],
    ])
    tracks = [local_file("A", 1), local_file("B", 2),
              local_file("Gamma", 3, disc=2), local_file("Delta", 4, disc=2)]
    tracks[2]["title"] = tracks[3]["title"] = ""
    for track, name in zip(tracks[2:], ("03", "04")):
        track.update(filename=f"{name}.flac", path=f"/music/Album/CD2/{name}.flac")
    audited = audit(official, tracks)

    assert [t["status"] for t in audited["tracks"]] == ["found"] * 4
    assert extras(audited) == []


def test_unique_title_matches_across_discs_without_disc_tags():
    audited = audit(two_by_two(), [local_file("Delta")])

    assert statuses(audited)[3] == (2, "2", "Delta", "found")


def test_repeated_title_on_another_disc_stays_missing():
    official = mb_release([
        [official_track(1, "Intro", recording="rec-intro-1"), official_track(2, "Song")],
        [official_track(1, "Intro", recording="rec-intro-2"), official_track(2, "Other")],
    ])
    audited = audit(official, [local_file("Intro", 1), local_file("Song", 2)])

    assert statuses(audited) == [
        (1, "1", "Intro", "found"), (1, "2", "Song", "found"),
        (2, "1", "Intro", "missing"), (2, "2", "Other", "missing"),
    ]


# --- Extras and wrong releases ----------------------------------------------------------


def test_unmatched_local_files_are_listed_but_not_counted():
    official = mb_release([[official_track(n, title) for n, title in
                            enumerate(["One", "Two", "Three", "Four"], 1)]])
    audited = audit(official, [local_file("One", 1), local_file("Hidden Bonus", 9)])

    assert extras(audited) == ["Hidden Bonus"]
    assert audited["total_tracks_expected"] == 4
    assert (audited["found_count"], audited["missing_count"]) == (1, 3)
    assert audited["completion_pct"] == 25.0


def test_release_matching_none_of_the_local_files_is_unverified():
    official = mb_release([[official_track(1, "Strumming Music")]], artist="Charlemagne Palestine",
                          title="Strumming")
    audited = audit(official, [local_file("Lantern", 1, artist="pencil")],
                    artist="pencil", title="Strumming")

    assert audited["is_audited"] is False
    assert audited["status"] == "unverified"
    assert audited["audit_error"] == NO_MATCH_ERROR


# --- Compilation credits ----------------------------------------------------------------


def test_single_artist_compilation_keeps_its_artist():
    official = mb_release([[official_track(n, title, artist="Lone") for n, title in
                            enumerate(["Crush Mood", "Chroma", "Temples"], 1)]],
                          artist="Lone", title="Ambivert Tools One – Four",
                          rg={"id": "rg", "primary-type": "Album", "secondary-type-list": ["Compilation"]})
    audited = audit(official, [local_file("Crush Mood", 1, artist="Lone")],
                    artist="Various Artists", title="Ambivert Tools One – Four", is_va=True)

    assert audited["artist"] == audited["album_artist"] == "Lone"
    assert audited["is_va"] is False
    assert audited["is_compilation"] is True
    assert {t["artist"] for t in audited["tracks"]} == {"Lone"}


def test_remix_compilation_of_the_credited_artist_keeps_its_artist():
    official = mb_release([[
        official_track(1, "Gloss (Machinedrum remix)", artist="Lando Kal"),
        official_track(2, "Wine (Machinedrum remix)", artist="Om Unit"),
        official_track(3, "Rapid", artist="Machinedrum"),
    ]], artist="Machinedrum", title="Nice Effort",
        rg={"id": "rg", "primary-type": "Compilation"})
    audited = audit(official, [local_file("Rapid", 3, artist="Machinedrum")],
                    artist="Machinedrum", title="Nice Effort")

    assert (audited["artist"], audited["is_va"]) == ("Machinedrum", False)


@pytest.mark.parametrize("credit", ["Various Artists", "ＶＡＲＩＯＵＳ", "V.A", "Gilles Peterson"])
def test_multi_artist_compilations_stay_various_artists(credit):
    official = mb_release([[
        official_track(1, "Rene Thomas", artist="Kyle Hall"),
        official_track(2, "Iyo Djeli", artist="Dele Sosimi"),
        official_track(3, "Sunrise", artist="Hidden Jazz Quartett"),
    ]], artist=credit, title="Bubblers Four",
        rg={"id": "rg", "primary-type": "Album", "secondary-type-list": ["Compilation"]})
    audited = audit(official, [local_file("Rene Thomas", 1, artist="Kyle Hall")],
                    artist=credit, title="Bubblers Four")

    assert (audited["artist"], audited["is_va"]) == ("Various Artists", True)
    assert audited["tracks"][0]["artist"] == "Kyle Hall"


# --- Title-only MusicBrainz search ------------------------------------------------------


def searched_audit(local_artist, local_tracks, candidates, title="Now You Know"):
    mb = MagicMock()
    mb.search_release.return_value = [
        {"id": release["id"], "title": release["title"],
         "artist-credit": release["artist-credit"]} for release in candidates
    ]
    mb.get_release_by_id.side_effect = lambda release_id, **_: next(
        release for release in candidates if release["id"] == release_id)
    service = LibraryReleaseService(mb_client=mb, slskd_client=MagicMock())
    audited = service.audit_release({"artist": local_artist, "title": title, "tracks": local_tracks})
    return audited, mb


def now_you_know(artist, release_id, titles):
    return mb_release([[official_track(n, t, recording=f"{release_id}-{n}") for n, t in enumerate(titles, 1)]],
                      release_id=release_id, artist=artist, title="Now You Know")


def test_title_search_rejects_another_artists_same_title_album():
    madame_mayhem = now_you_know("Madame Mayhem", "mayhem", ["Now You Know", "Little Miss", "Paralyzed"])
    audited, mb = searched_audit("Machinedrum", [
        local_file("Wishbone Be Broken", 2, artist="Machinedrum"),
        local_file("Are I", 12, artist="Machinedrum"),
    ], [madame_mayhem])

    assert audited["is_audited"] is False
    assert audited["status"] == "unverified"
    assert "other artists" in audited["audit_error"]
    assert audited.get("mb_release_id") is None
    mb.get_release_by_id.assert_not_called()


def test_title_search_accepts_a_spelling_variant_of_the_local_artist():
    madame_mayhem = now_you_know("Madame Mayhem", "mayhem", ["Now You Know", "Little Miss"])
    machine_drum = now_you_know("Machine Drum", "machine-drum",
                                ["Big Beauty Hose", "Wishbone Be Broken", "Drums of Steel"])
    audited, _ = searched_audit("Machinedrum", [
        local_file("Big Beauty Hose", 1, artist="Machinedrum"),
        local_file("Wishbone Be Broken", 2, artist="Machinedrum"),
    ], [madame_mayhem, machine_drum])

    assert audited["is_audited"] is True
    assert audited["mb_release_id"] == "machine-drum"
    assert audited["artist"] == "Machine Drum"
    assert [t["status"] for t in audited["tracks"]] == ["found", "found", "missing"]


def test_title_search_result_matching_no_local_track_is_unverified():
    # Same credit, different album: no local file is on its tracklist.
    same_artist = mb_release([[official_track(1, "Strumming Music")]], release_id="other",
                             artist="pencil", title="Strumming")
    audited, _ = searched_audit("pencil", [local_file("Lantern", 1, artist="pencil")],
                                [same_artist], title="Strumming")

    assert audited["is_audited"] is False
    assert audited["audit_error"] == NO_MATCH_ERROR


def test_title_search_prefers_the_candidate_covering_more_local_tracks():
    first = now_you_know("Machine Drum", "promo", ["Big Beauty Hose", "Fury"])
    second = now_you_know("Machine Drum", "album", ["Big Beauty Hose", "Wishbone Be Broken", "Fury"])
    audited, _ = searched_audit("Machine Drum", [
        local_file("Big Beauty Hose", 1, artist="Machine Drum"),
        local_file("Wishbone Be Broken", 2, artist="Machine Drum"),
    ], [first, second])

    assert audited["mb_release_id"] == "album"


def test_title_search_verifies_a_local_compilation_credited_to_its_curator():
    # MusicBrainz credits the compilation to Gilles Peterson; the local files name only
    # the performers. The fetched tracklist's credits confirm it.
    curated = mb_release([[
        official_track(1, "Rene Thomas", artist="Kyle Hall"),
        official_track(2, "Iyo Djeli", artist="Dele Sosimi"),
    ]], release_id="bubblers", artist="Gilles Peterson", title="Brownswood Bubblers Four",
        rg={"id": "rg", "primary-type": "Album", "secondary-type-list": ["Compilation"]})
    audited, _ = searched_audit("Various Artists", [
        local_file("Rene Thomas", 1, artist="Kyle Hall"),
        local_file("Iyo Djeli", 2, artist="Dele Sosimi"),
    ], [curated], title="Brownswood Bubblers Four")

    assert audited["is_audited"] is True
    assert audited["mb_release_id"] == "bubblers"
    assert audited["missing_count"] == 0


def test_title_search_rejects_a_compilation_none_of_whose_artists_are_local():
    other = mb_release([[
        official_track(1, "Rene Thomas", artist="Someone Else"),
        official_track(2, "Sunrise", artist="Another Artist"),
    ]], release_id="other", artist="Various Artists", title="Brownswood Bubblers Four")
    audited, _ = searched_audit("Various Artists", [
        local_file("Rene Thomas", 1, artist="Kyle Hall"),
    ], [other], title="Brownswood Bubblers Four")

    assert audited["is_audited"] is False
