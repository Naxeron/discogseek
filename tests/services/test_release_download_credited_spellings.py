"""Compilation tracks credited under another spelling of the requested artist.

Seen live: MusicBrainz credits kyou1110 as 'Kyou' on FUXX THE NEZUMITORI, while the
peer's files say 'kyou1110 - Hd.flac'. Soulseek matches whole words, so 'Kyou hd'
never finds them, and a filename naming 'kyou1110' looked like another artist.
"""

from unittest.mock import MagicMock

from discogseek.services.library_download import download_missing_tracks

FOLDER = r"music\_Compilations\2011-05 - Fuxx The Nezumitori [[none]]"
HD = FOLDER + r"\11 - kyou1110 - Hd.flac"
EMO = FOLDER + r"\03 - kyou1110 Featuring .kom - Emo.flac"


def client_answering(answers):
    """A client whose searches return files only for the queries in `answers`."""
    client = MagicMock()
    client.get_downloads.return_value = []
    seen = []

    def results(query):
        files = answers.get(query, [])
        return {"state": "Completed", "isComplete": True, "responses": [
            {"username": "sprite9421", "hasFreeUploadSlot": True, "queueLength": 0, "uploadSpeed": 1,
             "files": [{"filename": name, "size": 5_000_000} for name in files]}] if files else []}

    def search(query=None, **kwargs):
        seen.append(query)
        return results(query)

    def batch_search(queries, **kwargs):
        seen.extend(queries)
        return {query: results(query) for query in queries}

    client.search.side_effect = search
    client.batch_search.side_effect = batch_search
    return client, seen


def row(title, artist, number, names=("kyou1110", "amazinganguillajaponica")):
    track = {"title": title, "artist": artist, "disc_number": 1, "track_number": str(number),
             "mb_track_id": f"fuxx-{number}", "status": "missing"}
    if names is not None:
        track["artist_names"] = list(names)
    return track


def test_main_name_is_searched_for_a_track_credited_under_another_spelling():
    client, seen = client_answering({"kyou1110 hd": [HD]})

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "FUXX THE NEZUMITORI",
                                     [row("hd", "Kyou", 11)], dry_run=True, search_scope="track")

    assert result["matched_count"] == 1
    assert result["queued_files"][0]["filename"] == HD
    # Right after the credited spelling, before its punctuation variants.
    assert seen.index("kyou1110 hd") == seen.index("Kyou hd") + 1


def test_featured_guest_credit_matches_the_main_name_in_the_filename():
    client, _ = client_answering({"kyou1110 EMO": [EMO]})

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "FUXX THE NEZUMITORI",
                                     [row("EMO", "Kyou Featuring .kom", 3)], dry_run=True, search_scope="track")

    assert result["matched_count"] == 1


def test_artist_wide_search_uses_the_main_name_last():
    # Only the broad 'kyou1110' search (reused from discovery in artist downloads) lists the file.
    client, seen = client_answering({"kyou1110": [HD]})

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "FUXX THE NEZUMITORI",
                                     [row("hd", "Kyou", 11)], dry_run=True, search_scope="track")

    assert result["matched_count"] == 1
    assert seen.index("kyou1110") > seen.index("Kyou")


def test_without_other_names_a_filename_naming_another_artist_is_rejected():
    client, seen = client_answering({"Kyou hd": [HD]})

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "FUXX THE NEZUMITORI",
                                     [row("hd", "Kyou", 11, names=None)], dry_run=True, search_scope="track")

    assert result["matched_count"] == 0
    assert "kyou1110 hd" not in seen


def test_a_credit_already_naming_the_main_artist_adds_no_queries():
    client, seen = client_answering({})

    download_missing_tracks(client, MagicMock(), "Various Artists", "BITE 2 BEAT",
                            [row("She's unable to buy underpants", "kyou1110 + sabi_c", 12)],
                            dry_run=True, search_scope="track")

    assert not [query for query in seen if "amazinganguillajaponica" in query.casefold()]
    assert seen.count("kyou1110") == 1


THIS_IS_THE_HELL = r"06-DoujinCafe\Lilium Records\Free Releases\[LLMR-011] THIS IS THE HELL\DISC1"


def test_title_before_the_credited_artist_in_a_filename():
    # Peers number some compilations 'NN - Title - Artist'.
    path = THIS_IS_THE_HELL + r"\7 - KounagiHisyouKa (CM Mix) - Kyou1110.mp3"
    client, _ = client_answering({"Kyou1110 KounagiHisyouKa Cm Mix": [path]})

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "This Is The Hell",
                                     [row("KounagiHisyouKa (Cm Mix)", "Kyou1110", 7)], dry_run=True, search_scope="track")

    assert result["matched_count"] == 1


def test_a_trailing_part_naming_another_artist_is_not_the_credit():
    # 'Song - Other Artist' is not this row's song just because the title comes first.
    path = THIS_IS_THE_HELL + r"\7 - KounagiHisyouKa (CM Mix) - Somebody Else.mp3"
    client, _ = client_answering({"Kyou1110 KounagiHisyouKa Cm Mix": [path]})

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "This Is The Hell",
                                     [row("KounagiHisyouKa (Cm Mix)", "Kyou1110", 7)], dry_run=True, search_scope="track")

    assert result["matched_count"] == 0
