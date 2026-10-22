"""Peer filenames and MusicBrainz titles of doujin releases that title scoring misread."""

import pytest

from discogseek.core.text import title_match_score


@pytest.mark.parametrize("title, filename", [
    ("hd", "11 - kyou1110 - Hd.flac"),
    ("Live", "11 - Artist - Live.flac"),
    ("Remix", "A1. Artist - Remix.mp3"),
])
def test_a_title_that_is_a_version_word(title, filename):
    assert title_match_score(title, filename) == 1.0


def test_a_version_word_after_a_title_is_still_a_version():
    assert title_match_score("Live", "Artist - Song - Live.flac") == 0.0
    assert title_match_score("Song (Live)", "Artist - Song - Live.flac") == 1.0


@pytest.mark.parametrize("title, filename", [
    ("More Movement", "01 1. More Movement.mp3"),
    ("Ddddddisssccccccc", "13 13. Ddddddisssccccccc.mp3"),
])
def test_position_written_twice(title, filename):
    assert title_match_score(title, filename) == 1.0


def test_a_second_number_that_is_another_position_stays():
    assert title_match_score("5. Ganz", "05 6. Ganz.mp3") == 0.0
    assert title_match_score("2 Is 8", "05 2 Is 8.mp3") == 1.0


def test_masked_letters_match_any_letters_but_never_exactly():
    # Outside the release's folder an inexact title also needs its position.
    assert title_match_score("my fuXX Sound", "13 kyou1110 - My Fuck Sound.mp3") == 0.95
    assert title_match_score("f**k it", "02 - fuck it.mp3") == 0.95
    assert title_match_score("my fuXX Sound", "13. kyou1110 - my fuXX Sound.mp3") == 1.0


def test_a_trailing_asterisk_is_not_a_mask():
    assert title_match_score("star*", "07 Artist A - Stars.mp3") < 0.95


def test_a_mask_counts_letters():
    assert title_match_score("my fuXX Sound", "13 kyou1110 - My Fuzzy Sound.mp3") < 1.0


def test_an_all_x_word_is_not_a_mask():
    assert title_match_score("She say [XXXXXX]", "01. She say [ABCDEF].mp3") < 0.85


@pytest.mark.parametrize("title, filename", [
    ("ETS$STE", "01. kyou1110 - ETS$STE (Commission Song).mp3"),
    ("Senpuuki", "Senpuuki (Remix pack in Description).m4a"),
    ("Voyage 1969 (kyou1110 remix)", "01. Voyage 1969 (kyou1110 Remix)(Commission Song).mp3"),
    ("$SSTRSST", "01. 27 Kyou1110 - $SSTRSST [Remix PACK in Description].mp3"),
])
def test_uploader_notes_in_brackets_are_not_part_of_the_title(title, filename):
    assert title_match_score(title, filename) == 1.0


def test_uploader_notes_do_not_hide_versions():
    assert title_match_score("Senpuuki", "Senpuuki (DJ Foo Remix) (Remix pack in Description).mp3") == 0.0
    assert title_match_score("Voyage 1969", "01. Voyage 1969 (kyou1110 Remix)(Commission Song).mp3") == 0.0


def test_an_unbracketed_note_word_is_a_title():
    assert title_match_score("Commission", "kyou1110 - Commission.mp3") == 1.0
    assert title_match_score("kyou1110", "kyou1110 - Commission.mp3") < 0.9
