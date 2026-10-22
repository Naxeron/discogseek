"""Rare compilation tracks found only by short queries, and symbol-variant artist credits.

Seen live: peers answered 'Sepalcure Lose' and 'RJ Valeo' with the Cambio files
"All You've Got To Lose.mp3" and "You're the One.mp3", but not the full-title queries.
"""

from unittest.mock import MagicMock

from discogseek.services.library_download import download_missing_tracks

SEPALCURE = r"@@yuqfj\Music\Music\Sepalcure\Cambio\11 All You've Got To Lose.mp3"
VALEO = r"@@yuqfj\Music\Music\RJ Valeo\Cambio\10 You're the One.mp3"


def client_answering(answers):
    """A client whose searches return files only for the queries in `answers`."""
    client = MagicMock()
    client.get_downloads.return_value = []
    seen = []

    def results(query):
        files = answers.get(query, [])
        return {"state": "Completed", "isComplete": True, "responses": [
            {"username": "xLouie", "hasFreeUploadSlot": True, "queueLength": 0, "uploadSpeed": 1,
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


def row(title, artist, number):
    return {"title": title, "artist": artist, "disc_number": 1, "track_number": str(number),
            "mb_track_id": f"cambio-{number}", "status": "missing"}


def test_distinctive_title_words_find_a_file_the_full_title_misses():
    client, seen = client_answering({"Sepalcure Lose": [SEPALCURE]})

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "Cambio",
                                     [row("All You've Got To Lose", "Sepalcure", 11)], dry_run=True)

    assert result["matched_count"] == 1
    assert "Sepalcure Lose" in seen and seen.index("Sepalcure Lose") > seen.index("Sepalcure All You've Got To Lose")


def test_artist_alone_is_the_last_resort_for_a_title_without_distinctive_words():
    client, seen = client_answering({"RJ Valeo": [VALEO]})

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "Cambio",
                                     [row("You're The One", "RJ Valeo", 10)], dry_run=True)

    assert result["matched_count"] == 1
    assert seen[-1] == "RJ Valeo"


def test_symbol_variants_of_an_artist_credit_match():
    # MusicBrainz credits 'sato❤shin'; the peer's file says 'sato♥shin'.
    peer_file = r"MUSIC\Extra\lolicore.jp\Reinterpret My Roots\23. sato♥shin - 22 8.mp3"
    client, _ = client_answering({"Reinterpret My Roots": [peer_file]})

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "Reinterpret My Roots",
                                     [row("22/8", "sato❤shin", 23)], dry_run=True)

    assert result["matched_count"] == 1


def test_artist_wide_results_need_the_exact_title():
    # The artist's other song 'Dreams' must not stand for the requested 'Dream'.
    other = r"Music\Lorem Ipsum\Other Album (2012)\03 - Dreams.flac"
    client, seen = client_answering({"Lorem Ipsum": [other], "Lorem Ipsum Dream": []})

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "Night Songs",
                                     [row("Dream", "Lorem Ipsum", 3)], dry_run=True)

    assert result["matched_count"] == 0
    assert "Lorem Ipsum" in seen


def test_placeholder_credits_are_never_searched_alone():
    client, seen = client_answering({})

    download_missing_tracks(client, MagicMock(), "Various Artists", "Dub Plates",
                            [row("Dub", "[unknown]", 5)], dry_run=True)

    assert "unknown" not in [query.casefold() for query in seen]


def test_an_albums_own_artist_is_not_searched_alone():
    # The album search already covered the album artist; only guests get artist-wide searches.
    client, seen = client_answering({})

    download_missing_tracks(client, MagicMock(), "Lone", "Hyperphantasia",
                            [row("Triton", "Lone", 6), row("Big World", "Lone feat. Lou Hayter", 10)], dry_run=True)

    assert "Lone" not in seen and "Lone Triton" in seen


def test_fallback_runs_after_the_album_search_in_track_scope():
    client, seen = client_answering({})

    download_missing_tracks(client, MagicMock(), "Various Artists", "Cambio",
                            [row("You're The One", "RJ Valeo", 10)], dry_run=True, search_scope="track")

    assert seen.index("RJ Valeo") > seen.index("Cambio")
