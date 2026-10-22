"""Album placeholders and Various Artists spellings."""

import pytest

from discogseek.core.release_metadata import is_unknown_album, is_various_artists


@pytest.mark.parametrize("title", [
    "[standalone recordings]", "[non-album tracks]", "Non-Album Tracks", "Unknown", "[Unknown Album]", "",
])
def test_picard_and_server_placeholders_are_not_album_titles(title):
    assert is_unknown_album(title) is True


@pytest.mark.parametrize("title", ["Untitled", "Unknown Pleasures", "Standalone", "Tracks"])
def test_real_titles_are_albums(title):
    assert is_unknown_album(title) is False


@pytest.mark.parametrize("name", ["ＶＡＲＩＯＵＳ", "V.A", "V.A.", "v. a.", "Ｖ.Ａ.", "Various Artists", "VA"])
def test_various_artists_spellings(name):
    assert is_various_artists(name) is True


@pytest.mark.parametrize("name", ["Vangelis", "V", "Lone", "Vatican Shadow"])
def test_artist_names_are_not_various_artists(name):
    assert is_various_artists(name) is False
