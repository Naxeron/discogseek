"""Decorative symbols inside names normalize the same way whichever symbol a tagger chose."""

import pytest

from discogseek.core.text import normalize_text


@pytest.mark.parametrize("credit", ["sato❤shin", "sato♥shin", "sato♡shin", "sato★shin"])
def test_decorative_symbols_inside_a_name_are_separators(credit):
    # MusicBrainz credits 'sato❤shin'; a peer folder names 'sato♥shin', which ASCII
    # folding used to spell out as 'satoheartsshin'.
    assert normalize_text(credit) == "sato shin"


def test_letter_like_symbols_keep_their_letters():
    assert normalize_text("ギコ猫 ⓈⒺⓁⒺⒸⓉⒾⓄⓃ") == normalize_text("ギコ猫 SELECTION")


@pytest.mark.parametrize("title", ["★", "∞", "♪♪"])
def test_symbol_only_titles_keep_their_symbol_key(title):
    assert normalize_text(title) and normalize_text(title) != normalize_text("")


def test_mixed_compatibility_symbols_keep_their_letters():
    assert normalize_text("37.0℃") == normalize_text("37.0°C")
    assert normalize_text("x⒜") == "x a"
