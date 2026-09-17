"""Roman numeral normalization and preservation of numeric song titles."""

import pytest

from discogseek.core.text import (
    normalize_roman_numerals,
    normalize_text,
    strip_track_number_and_artist,
)


class TestTitleNumbers:
    @pytest.mark.parametrize("roman_title,arabic_title", [
        ("Part IV", "Part 4"),
        ("Part IX", "Part 9"),
        ("Part XIV", "Part 14"),
        ("Part XX", "Part 20"),
        ("Act IX", "Act 9"),
        ("Act III", "Act 3"),
        ("Movement VII", "Movement 7"),
        ("Mov. II", "Mov 2"),
        ("Vol. III", "Vol 3"),
        ("Volume V", "Volume 5"),
        ("Chapter XV", "Chapter 15"),
        ("Suite No. II", "Suite No 2"),
        ("Opus XII", "Opus 12"),
        ("Canto VIII", "Canto 8"),
    ])
    def test_roman_numeral_with_indicators(self, roman_title: str, arabic_title: str):
        """Roman numerals with indicators convert to Arabic digits and normalize identically."""
        norm_roman = normalize_text(roman_title)
        norm_arabic = normalize_text(arabic_title)
        assert norm_roman == norm_arabic, f"Mismatch: '{norm_roman}' != '{norm_arabic}'"

    @pytest.mark.parametrize("bracketed_roman,expected_arabic", [
        ("(I)", "(1)"),
        ("[IV]", "[4]"),
        ("{V}", "{5}"),
        ("(IX)", "(9)"),
        ("[XII]", "[12]"),
        ("(XIX)", "(19)"),
    ])
    def test_bracketed_roman_numerals(self, bracketed_roman: str, expected_arabic: str):
        """Bracketed Roman numerals (even single letters I and V) convert to Arabic."""
        converted = normalize_roman_numerals(bracketed_roman)
        assert converted == expected_arabic

    @pytest.mark.parametrize("unambiguous_roman,expected_arabic", [
        ("II", "2"),
        ("III", "3"),
        ("IV", "4"),
        ("VI", "6"),
        ("VII", "7"),
        ("VIII", "8"),
        ("IX", "9"),
        ("XI", "11"),
        ("XII", "12"),
        ("XIV", "14"),
        ("XV", "15"),
        ("XVI", "16"),
        ("XVII", "17"),
        ("XVIII", "18"),
        ("XIX", "19"),
        ("XX", "20"),
    ])
    def test_standalone_unambiguous_roman_numerals(self, unambiguous_roman: str, expected_arabic: str):
        """Standalone unambiguous Roman numerals (II through XX) convert to Arabic numbers."""
        converted = normalize_roman_numerals(unambiguous_roman)
        assert converted == expected_arabic

    @pytest.mark.parametrize("protected_title", [
        "I Love You",
        "Am I Wrong",
        "I Will Always Love You",
        "I",
        "Generation V",
        "V for Vendetta",
        "V",
        "Planet X",
        "Project X",
        "X",
        "Six Degrees",
        "Mix Tape",
        "Fix You",
        "Tax Man",
        "Exit Music",
        "Vivid Colors",
    ])
    def test_pronouns_and_common_words_preserved(self, protected_title: str):
        """Single-letter pronouns ('I', 'V', 'X') and words like 'six', 'mix' are NEVER corrupted to digits."""
        converted = normalize_roman_numerals(protected_title)
        # Check that 'I', 'V', 'X' did not turn into '1', '5', '10'
        if protected_title == "I":
            assert converted == "I"
        elif protected_title == "I Love You":
            assert "1" not in converted and "I" in converted
        elif protected_title == "Am I Wrong":
            assert "1" not in converted
        elif protected_title == "Generation V":
            assert "5" not in converted
        elif protected_title == "Planet X":
            assert "10" not in converted
        elif protected_title in ("Six Degrees", "Mix Tape", "Fix You", "Tax Man", "Exit Music", "Vivid Colors"):
            assert not any(d in converted for d in "1234567890")

    @pytest.mark.parametrize("roman_str,arabic_str", [
        ("Part I", "Part 1"),
        ("Part II", "Part 2"),
        ("Part III", "Part 3"),
        ("Part IV", "Part 4"),
        ("Part V", "Part 5"),
        ("Part VI", "Part 6"),
        ("Part VII", "Part 7"),
        ("Part VIII", "Part 8"),
        ("Part IX", "Part 9"),
        ("Part X", "Part 10"),
        ("Part XI", "Part 11"),
        ("Part XII", "Part 12"),
        ("Part XIII", "Part 13"),
        ("Part XIV", "Part 14"),
        ("Part XV", "Part 15"),
        ("Part XVI", "Part 16"),
        ("Part XVII", "Part 17"),
        ("Part XVIII", "Part 18"),
        ("Part XIX", "Part 19"),
        ("Part XX", "Part 20"),
        # Lowercase indicators & numerals
        ("part i", "part 1"),
        ("part iv", "part 4"),
        ("part ix", "part 9"),
        ("part xiv", "part 14"),
        ("part xx", "part 20"),
        # Other indicators
        ("Act I", "Act 1"),
        ("Act IX", "Act 9"),
        ("Movement III", "Movement 3"),
        ("Mov. IV", "Mov 4"),
        ("Mvt V", "Mvt 5"),
        ("Vol. II", "Vol 2"),
        ("Volume X", "Volume 10"),
        ("Suite No. IV", "Suite No 4"),
        ("Opus XII", "Opus 12"),
        ("Op. III", "Op 3"),
        ("Section VI", "Section 6"),
        ("Scene VII", "Scene 7"),
        ("Chapter VIII", "Chapter 8"),
    ])
    def test_roman_indicators_convert_to_arabic(self, roman_str: str, arabic_str: str):
        """Roman numerals with indicators convert to Arabic numbers identically."""
        norm_r = normalize_text(roman_str)
        norm_a = normalize_text(arabic_str)
        assert norm_r == norm_a, f"Mismatch: '{norm_r}' != '{norm_a}'"

    @pytest.mark.parametrize("bracketed,expected", [
        ("Song Title (I)", "Song Title (1)"),
        ("Song Title [II]", "Song Title [2]"),
        ("Song Title {III}", "Song Title {3}"),
        ("Song Title (IV)", "Song Title (4)"),
        ("Song Title [V]", "Song Title [5]"),
        ("Song Title (VI)", "Song Title (6)"),
        ("Song Title [VII]", "Song Title [7]"),
        ("Song Title (VIII)", "Song Title (8)"),
        ("Song Title [IX]", "Song Title [9]"),
        ("Song Title (X)", "Song Title (10)"),
        ("Song Title [XIV]", "Song Title [14]"),
        ("Song Title (XIX)", "Song Title (19)"),
        ("Song Title [XX]", "Song Title [20]"),
    ])
    def test_bracketed_roman_numerals_convert(self, bracketed: str, expected: str):
        """Bracketed Roman numerals convert accurately."""
        assert normalize_text(bracketed) == normalize_text(expected)

    @pytest.mark.parametrize("word", [
        "I",
        "I Am",
        "V",
        "V For Vendetta",
        "X",
        "Planet X",
        "Six",
        "Mix",
        "Fix",
        "Exit Music",
        "Vivid",
        "Livin' La Vida",
        "Matrix",
        "Civilization",
        "Maximum",
        "Taxation",
        "Oxidation",
    ])
    def test_common_words_and_pronouns_not_corrupted(self, word: str):
        """English words and pronouns containing Roman characters are never converted to digits."""
        converted = normalize_roman_numerals(word)
        assert not any(d in converted for d in "0123456789"), f"Word '{word}' was corrupted to '{converted}'"

    def test_short_song_titles_not_stripped(self):
        """Short numeric/alphanumeric song titles are never mangled."""
        assert strip_track_number_and_artist("01 - 1999.flac") == "1999"
        assert strip_track_number_and_artist("02 - 1984.mp3") == "1984"
        assert strip_track_number_and_artist("03 - 21.flac") == "21"
        assert strip_track_number_and_artist("04 - One.flac") == "One"
        assert strip_track_number_and_artist("05 - 3D.flac") == "3D"
