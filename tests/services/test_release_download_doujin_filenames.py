"""Doujin compilation copies whose names or MusicBrainz titles the matcher misread.

All seen live while completing kyou1110's discography: underscore-only Edsillfor rips,
positions written twice, credit notes in MusicBrainz credits, remixes MusicBrainz titles
like the original, a live set whose date runs into 'Live', and masked words.
"""

from unittest.mock import MagicMock

from discogseek.services.library_download import download_missing_tracks

KYOU = ["kyou1110", "amazinganguillajaponica"]


def client_answering(answers):
    """A client whose searches return (user, path, length) files only for the queries in `answers`."""
    client = MagicMock()
    client.get_downloads.return_value = []
    seen = []

    def results(query):
        by_user = {}
        for user, path, length in answers.get(query, []):
            by_user.setdefault(user, []).append({"filename": path, "size": 5_000_000, "length": length})
        return {"state": "Completed", "isComplete": True, "responses": [
            {"username": user, "hasFreeUploadSlot": True, "queueLength": 0, "uploadSpeed": 1, "files": files}
            for user, files in by_user.items()]}

    def search(query=None, **kwargs):
        seen.append(query)
        return results(query)

    def batch_search(queries, **kwargs):
        seen.extend(queries)
        return {query: results(query) for query in queries}

    client.search.side_effect = search
    client.batch_search.side_effect = batch_search
    return client, seen


def row(title, artist, number, duration=None, names=KYOU, track_id=None):
    track = {"title": title, "artist": artist, "disc_number": 1, "track_number": str(number),
             "mb_track_id": track_id or f"track-{number}", "status": "missing", "artist_names": list(names)}
    if duration:
        track["duration"] = duration
    return track


def test_underscores_separating_position_artist_and_title():
    path = r"ln\Edsillfor Recordings\[ELF-030] Various - DigitaditionalMusics\04_kyou1110_Izumikami.mp3"
    client, _ = client_answering({"Kyou1110 Izumikami": [("cloverpiral", path, 205)]})

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "DigitaditionalMusics",
                                     [row("Izumikami", "Kyou1110", 4)], dry_run=True, search_scope="track")

    assert result["matched_count"] == 1


def test_underscored_title_without_the_credited_artist_is_not_split():
    # 'You_Can_t_See_the_Love' must not lose words until a shorter title matches.
    path = r"ln\Edsillfor Recordings\[ELF-008] lililL - You Can't See the Love EP\06_You_Can_t_See_the_Love.mp3"
    client, _ = client_answering({"lililL Love": [("cloverpiral", path, 200)]})

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "Other Compilation",
                                     [row("Love", "lililL", 6, names=())], dry_run=True, search_scope="track")

    assert result["matched_count"] == 0


def test_position_written_twice():
    path = r"musicorganizedforrealthistime\Various\Future Disorder [3 Years Compilation]\13 13. $SSTFNMZ.mp3"
    client, _ = client_answering({"Future Disorder 3 Years Compilation": [("geqgiuq", path, 230)]})

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "Future Disorder 3 Years Compilation",
                                     [row("SSSTFNMZ", "kyou1110", 13, duration=230)], dry_run=True)

    assert result["matched_count"] == 1


def test_credit_note_is_left_out_of_searches():
    path = r"shared\soundcloud_v2\@kyou1110 [776672]\@kyou1110 - She say [XXXXXX] (2015)\01. She say [XXXXXX].mp3"
    client, seen = client_answering({"kyou1110 She say XXXXXX": [("pixelmelt", path, 180)]})

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "霊像崇拝",
                                     [row("She say [XXXXXX]", "kyou1110 (Ghost:ねえ、お願い事があるの)", 5, duration=180)],
                                     dry_run=True, search_scope="track")

    assert result["matched_count"] == 1
    assert not [query for query in seen if "Ghost" in query]


def test_remix_titled_like_the_original_on_a_remix_single():
    path = r"_\廻転楕円体\廻転楕円体 - 双頭の零\06廻転楕円体 - 双頭の零 (kyou1110 Remix).mp3"
    client, _ = client_answering({"kyou1110 双頭の零": [("Tech Chipper", path, 267)]})

    result = download_missing_tracks(client, MagicMock(), "廻転楕円体", "双頭の零",
                                     [row("双頭の零", "kyou1110 feat. ONE", 6, duration=267)],
                                     dry_run=True, search_scope="track")

    assert result["matched_count"] == 1


def test_another_artists_remix_is_not_the_track_of_an_album():
    # A guest's song on an album is not the same-titled remix of another release.
    path = r"Music\Somebody\Single\01 Somebody - Night Drive (Guest Remix).mp3"
    client, _ = client_answering({"Guest Night Drive": [("peer", path, 200)]})

    result = download_missing_tracks(client, MagicMock(), "Somebody", "Long Album",
                                     [row("Night Drive", "Guest", 6, names=())], dry_run=True, search_scope="track")

    assert result["matched_count"] == 0


def test_live_set_whose_date_runs_into_live():
    folder = r"ln\Edsillfor Recordings\[ELF-MIX001] Kyou1110 2011,12,11 Live at Fukuoka"
    path = folder + r"\Kyou1110 2011,12,11Live at Fukuoka.mp3"
    client, _ = client_answering({"kyou1110 Kyou1110 2011 12 11Live at Fukuoka": [("cloverpiral", path, 1799)]})

    result = download_missing_tracks(client, MagicMock(), "kyou1110", "Kyou1110 2011/12/11Live at Fukuoka",
                                     [row("Kyou1110 2011/12/11Live at Fukuoka", "kyou1110", 1, duration=1799)],
                                     dry_run=True)

    assert result["matched_count"] == 1


def test_masked_word_matches_the_spelled_out_word():
    folder = r"D-Da\D.S.K Compilation\Dasaku Compilation Album"
    client, _ = client_answering({"DASAKU COMPILATION ALBUM": [("Tech Chipper", folder + r"\13 kyou1110 - My Fuck Sound.mp3", 162)]})

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "DASAKU COMPILATION ALBUM",
                                     [row("my fuXX Sound", "kyou1110", 13, duration=162)], dry_run=True)

    assert result["matched_count"] == 1


def test_a_trailing_remix_credit_is_not_the_artist_after_the_title():
    # 'Title - sabi_c Remix' names a remix by a co-credited artist, not the credit.
    path = r"Music\Remixes\She's unable to buy underpants - sabi_c Remix.mp3"
    client, _ = client_answering({"kyou1110 She's unable to buy underpants": [("p", path, 200)]})

    result = download_missing_tracks(client, MagicMock(), "miso-nicomi records", "BITE 2 BEAT",
                                     [row("She's unable to buy underpants", "kyou1110 + sabi_c", 12)],
                                     dry_run=True, search_scope="track")

    assert result["matched_count"] == 0


def test_only_the_remixers_own_remix_stands_for_a_remix_titled_like_the_original():
    client, _ = client_answering({"kyou1110 双頭の零": [
        ("p", r"Music\kyou1110\kyou1110 - 双頭の零 (kyou1110 VIP Remix).mp3", 267),
        ("q", r"Music\kyou1110\kyou1110 - 双頭の零 (kyou1110 & sabi_c Remix).mp3", 267),
    ]})

    result = download_missing_tracks(client, MagicMock(), "廻転楕円体", "双頭の零",
                                     [row("双頭の零", "kyou1110 feat. ONE", 6, duration=267)],
                                     dry_run=True, search_scope="track")

    assert result["matched_count"] == 0


def test_a_self_remix_is_not_the_title_track_credited_under_another_spelling():
    path = r"Music\kyou1110\Remixes\kyou1110 - Senpuuki (kyou1110 Remix).mp3"
    client, _ = client_answering({"Kyou Senpuuki": [("p", path, 200)], "kyou1110 Senpuuki": [("p", path, 200)]})

    result = download_missing_tracks(client, MagicMock(), "kyou1110", "Senpuuki",
                                     [row("Senpuuki", "Kyou", 1)], dry_run=True, search_scope="track")

    assert result["matched_count"] == 0


def test_a_version_bracket_naming_a_band_stays():
    # The band Live's '(Live)' version is not a credit note.
    studio = r"Music\Live\Throwing Copper\05 Live - Lightning Crashes.mp3"
    client, _ = client_answering({"Live Lightning Crashes Live": [("p", studio, 300)],
                                  "Live Lightning Crashes": [("p", studio, 300)]})

    result = download_missing_tracks(client, MagicMock(), "Live", "Lightning Crashes",
                                     [row("Lightning Crashes (Live)", "Live", 1, names=())],
                                     dry_run=True, search_scope="track")

    assert result["matched_count"] == 0


def test_a_circle_in_brackets_stays_in_the_searches():
    client, seen = client_answering({})

    download_missing_tracks(client, MagicMock(), "Various Artists", "Touhou Compilation",
                            [row("Cirno no Perfect Sansuu Kyoushitsu", "ARM (IOSYS)", 3, names=())],
                            dry_run=True, search_scope="track")

    assert "ARM IOSYS" in seen


def test_a_digit_before_live_is_a_live_release_only_in_a_date():
    # 'Born2Live' is a song, not a live recording: a DJ-mix folder still cannot supply it.
    path = r"Music\BBC Radio 1 Essential Mix\2012-05-05 Artist A\03 Artist A - Born2Live.mp3"
    client, _ = client_answering({"Artist A Born2Live": [("p", path, 200)]})

    result = download_missing_tracks(client, MagicMock(), "Artist A", "Album",
                                     [row("Born2Live", "Artist A", 3, names=())], dry_run=True, search_scope="track")

    assert result["matched_count"] == 0


def test_whole_release_verification_reads_underscore_filenames():
    folder = r"ln\Edsillfor Recordings\[ELF-030] Various - DigitaditionalMusics"
    names = ["01_Inward_as_DJ_Pizz_optimum_catch.mp3", "02_lililL_Kirakira.mp3", "04_kyou1110_Izumikami.mp3"]
    official = [row("optimum catch", "Inward as DJ Pizz", 1, names=()), row("Kirakira", "lililL", 2, names=()),
                row("Izumikami", "Kyou1110", 4)]
    client, _ = client_answering({"DigitaditionalMusics": [("cloverpiral", f"{folder}\\{name}", 200) for name in names]})

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "DigitaditionalMusics", [official[2]],
                                     dry_run=True, complete_release_tracks=official)

    assert result["complete_release_available"] and result["matched_count"] == 1


def test_the_credited_artist_after_the_title_is_artist_evidence():
    # No release folder, length or other track says it: only the trailing 'Kyou1110'.
    path = r"Music\misc\7 - KounagiHisyouKa (CM Mix) - Kyou1110.mp3"
    client, _ = client_answering({"Kyou1110 KounagiHisyouKa Cm Mix": [("p", path, None)]})

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "This Is The Hell",
                                     [row("KounagiHisyouKa (Cm Mix)", "Kyou1110", 7)], dry_run=True, search_scope="track")

    assert result["matched_count"] == 1


def test_release_name_in_brackets_after_the_title():
    folder = r"shared\soundcloud_v2\@kyou1110 [776672]\@kyou1110 - FBD [MW.-Mosaic World-] (2017) [WEB-MP3] [128kbps] [309558716]"
    client, _ = client_answering({"kyou1110 F B D": [("pixelmelt", folder + r"\01. FBD [MW.-Mosaic World-].mp3", 207)]})

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "MW.-Mosaic World-",
                                     [row("F>B>D", "kyou1110", 10, duration=207)], dry_run=True, search_scope="track")

    assert result["matched_count"] == 1


def test_a_bracket_naming_another_release_stays_in_the_title():
    path = r"shared\soundcloud_v2\@kyou1110\01. FBD [Other Release].mp3"
    client, _ = client_answering({"kyou1110 F B D": [("pixelmelt", path, 207)]})

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "MW.-Mosaic World-",
                                     [row("F>B>D", "kyou1110", 10, duration=207)], dry_run=True, search_scope="track")

    assert result["matched_count"] == 0
