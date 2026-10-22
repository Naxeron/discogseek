"""Real peer filename shapes that the downloader and artist-mode matcher must accept or reject."""

from unittest.mock import MagicMock

import pytest

from discogseek.services.candidates import PeerCandidateIndex, find_best_track_candidate
from discogseek.services.library_download import download_missing_tracks


def matched_rows(artist, release, track, filename, scope):
    client = MagicMock()
    client.get_downloads.return_value = []
    response = {"responses": [{"username": "peer", "files": [{"filename": filename, "size": 1000}]}]}
    client.search.return_value = response
    client.batch_search.side_effect = lambda queries, **kwargs: {query: response for query in queries}
    result = download_missing_tracks(client, MagicMock(), artist, release, [track], dry_run=True, search_scope=scope)
    return result["matched_count"]


@pytest.mark.parametrize("scope", ["release", "track"])
@pytest.mark.parametrize("artist, release, track, filename", [
    # gliczide's share decorates every file with format and bitrate tags.
    ("Lone", "The Light Year Wave Tape", {"title": "Cody", "track_number": "7"},
     r"Music\Lone\2020 - The Light Year Wave Tape [Album]\07 - Cody [FLAC] [889 kbps].flac"),
    ("Lone", "Lemurian", {"title": "Koran Angel", "track_number": "1"},
     r"Lone\2008 - Lemurian [Album]\01 - Koran Angel [FLAC] [754 kbps].flac"),
    ("Machinedrum", "Psyconia", {"title": "Only One", "track_number": "1"},
     r"Machinedrum\Psyconia (Explicit)\01. Only One (Explicit).flac"),
    ("Lone", "Emerald Fantasy Tracks", {"title": "Big World", "track_number": "5"},
     r"Lone\Emerald Fantasy Tracks\05 - Big World - 2012 Remaster.flac"),
    ("Lone", "Emerald Fantasy Tracks", {"title": "Big World", "track_number": "5"},
     r"mp3\Lone-Emerald_Fantasy_Tracks-WEB-2012-FTD\05-lone-big_world.mp3"),
    ("Lone", "Emerald Fantasy Tracks", {"title": "Big World", "track_number": "5"},
     r"mp3\Lone - Emerald Fantasy Tracks\05_-_Lone_-_Big_World.mp3"),
    ("Artist", "Album", {"title": "Title Song", "track_number": "7"},
     r"Artist\Album\07 - Title Song (feat. Oliver Tree).flac"),
    ("Artist", "Album", {"title": "Title Song", "track_number": "7"},
     r"Artist\Album\07 - Title Song (feat. Clive).flac"),
    ("Artist", "Album", {"title": "Feathers", "track_number": "2"}, r"Artist\Album\02 - Feathers.flac"),
    ("Artist", "Album", {"title": "FTA", "track_number": "2"}, r"Artist\Album\02 - FTA.flac"),
    ("Artist", "Album", {"title": "Lyrics", "track_number": "4"}, r"Artist\Album\04 - Lyrics.flac"),
    ("Artist", "Album", {"title": "Vinyl", "track_number": "2"}, r"Artist\Album\02 - Vinyl.flac"),
    ("Artist", "Album", {"title": "Title", "track_number": "1"}, r"Artist\Album\01 Title.aif"),
    ("M83", "Hurry Up, We're Dreaming", {"title": "Midnight City", "track_number": "2"},
     r"M83 - Hurry Up, We're Dreaming\M83 - Midnight City.flac"),
    ("U2", "Achtung Baby", {"title": "One", "track_number": "3"}, r"U2\Achtung Baby\U2 - One.flac"),
    ("David Bowie", "Blackstar", {"title": "★", "track_number": "1"}, r"Music\David Bowie - Blackstar\01 - ★.flac"),
])
def test_decorated_peer_filenames_match(scope, artist, release, track, filename):
    assert matched_rows(artist, release, track, filename, scope) == 1


@pytest.mark.parametrize("scope", ["release", "track"])
@pytest.mark.parametrize("track, filename", [
    ({"title": "Cob", "track_number": "5"}, r"Artist\Album\05 - Cobweb.flac"),
    ({"title": "Trip", "track_number": "7"}, r"Artist\Album\07 - T.flac"),
])
def test_format_words_inside_titles_never_admit_other_songs(scope, track, filename):
    assert matched_rows("Artist", "Album", track, filename, scope) == 0


def test_artist_mode_does_not_take_drip_for_another_song():
    index = PeerCandidateIndex({
        ("peer", r"Music\Lone\Lone - Galaxy Garden"): {
            "matched_search_files": [
                {"filename": r"Music\Lone\Lone - Galaxy Garden\03 - I'd Rather.flac"},
                {"filename": r"Music\Lone\Lone - Galaxy Garden\07 - Don't Stop.flac"},
            ],
        },
    })

    assert find_best_track_candidate(index, "Drip", {"Lone"}) is None
    assert find_best_track_candidate(index, "Trip", {"Lone"}) is None
