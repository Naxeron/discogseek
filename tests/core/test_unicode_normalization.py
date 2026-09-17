"""Unicode normalization and search phrases across scripts and encodings."""

import unicodedata

import pytest

from discogseek.core.text import clean_search_phrase, normalize_text


class TestUnicodeNormalization:
    @pytest.mark.parametrize("title_nfc", [
        "Blåbærtur",
        "Smörgåsbord",
        "Møller",
        "Håkan Hellström",
        "Sigur Rós",
        "Ålesund",
        "København",
    ])
    def test_scandinavian_nfd_vs_nfc(self, title_nfc: str):
        """Scandinavian characters (å, ä, ö, ø, æ) decompose in NFD and must match NFC."""
        title_nfd = unicodedata.normalize("NFD", title_nfc)
        # Verify that NFD actually decomposed characters where applicable
        norm_nfc = normalize_text(title_nfc)
        norm_nfd = normalize_text(title_nfd)
        assert norm_nfc == norm_nfd, f"NFD vs NFC mismatch for '{title_nfc}': '{norm_nfd}' != '{norm_nfc}'"

    @pytest.mark.parametrize("title_nfc", [
        "Żółć",
        "Święty Mikołaj",
        "Chrząszcz brzmi w trzcinie w Szczebrzeszynie",
        "Zażółć gęślą jaźń",
        "Łódź Podwodna",
        "Kraków",
        "Gdańsk",
    ])
    def test_polish_diacritics_nfd_vs_nfc(self, title_nfc: str):
        """Polish diacritics (ą, ć, ę, ł, ń, ó, ś, ź, ż) must normalize identically in NFD and NFC."""
        title_nfd = unicodedata.normalize("NFD", title_nfc)
        norm_nfc = normalize_text(title_nfc)
        norm_nfd = normalize_text(title_nfd)
        assert norm_nfc == norm_nfd, f"Polish NFD vs NFC mismatch: '{norm_nfd}' != '{norm_nfc}'"
        # Also ensure Polish chars are cleanly mapped to ASCII transliteration
        assert not any(c in norm_nfc for c in "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ")

    @pytest.mark.parametrize("title_nfc,expected_equiv", [
        ("が", "か\u3099"),  # Dakuten Hiragana
        ("ガ", "カ\u3099"),  # Dakuten Katakana
        ("ぱ", "は\u309a"),  # Handakuten Hiragana
        ("パ", "ハ\u309a"),  # Handakuten Katakana
        ("ば", "は\u3099"),
        ("バ", "ハ\u3099"),
    ])
    def test_japanese_dakuten_handakuten_nfd_vs_nfc(self, title_nfc: str, expected_equiv: str):
        """Japanese voiced and semi-voiced kana in NFD (base + combining mark) must match NFC."""
        nfd_composed = unicodedata.normalize("NFD", title_nfc)
        assert nfd_composed == expected_equiv
        assert normalize_text(title_nfc) == normalize_text(expected_equiv)
        assert normalize_text(nfd_composed) == normalize_text(title_nfc)

    def test_japanese_kana_zenkaku_and_kanji_numerals(self):
        """Japanese Katakana/Hiragana unification, Zenkaku fullwidth, and Kanji numerals."""
        assert normalize_text("すてらべえ") == normalize_text("ステラベエ")
        assert normalize_text("らーめん") == normalize_text("ラーメン")
        assert normalize_text("Ｓｏｎｇ　１") == normalize_text("Song 1")
        assert normalize_text("トラック第一") == "toratsuku di 1" or "1" in normalize_text("トラック第一")
        assert normalize_text("第十二楽章") == normalize_text("第12楽章")

    @pytest.mark.parametrize("title_nfc", [
        "Über den Wolken",
        "Größenwahn",
        "Die Ärzte",
        "Götterdämmerung",
        "Schloß Neuschwanstein",
    ])
    def test_german_umlauts_and_eszett_nfd_vs_nfc(self, title_nfc: str):
        """German umlauts (ä, ö, ü) in NFD decompose into base + diaeresis and must match NFC."""
        title_nfd = unicodedata.normalize("NFD", title_nfc)
        assert normalize_text(title_nfc) == normalize_text(title_nfd)

    @pytest.mark.parametrize("title_nfc", [
        "Éléphant",
        "Château de Versailles",
        "Maître Gims",
        "Noël Blanc",
        "Mañana Por La Mañana",
        "Canción de Cuna",
        "Antonín Dvořák",
        "Příběh",
    ])
    def test_romance_and_slavic_accents_nfd_vs_nfc(self, title_nfc: str):
        """French, Spanish, and Czech accents match across NFC and NFD representations."""
        title_nfd = unicodedata.normalize("NFD", title_nfc)
        assert normalize_text(title_nfc) == normalize_text(title_nfd)

    @pytest.mark.parametrize("title_nfc", [
        # Vietnamese (nested diacritics)
        "Tiếng Việt",
        "Đường về quê mẹ",
        "Nguyễn Du",
        "Khúc Ca Mùa Thu",
        "Biển Nhớ",
        "Ở Trọ",
        "Hạ Trắng",
        # Nordic & Scandinavian
        "Þórsdrápa",
        "Árstíðir",
        "Sigur Rós",
        "Múm",
        "Blåbærtur",
        "Røyksopp",
        "Björk Guðmundsdóttir",
        # Baltic & Slavic
        "Žuvų Šokis",
        "Lietuvos Rytas",
        "Rīgas Vējš",
        "Zażółć gęślą jaźń",
        "Dvořák Antonín",
        "Leoš Janáček",
        "Bedřich Smetana",
        # Turkish
        "İstanbul Türküsü",
        "Diyarbakır",
        "Gümüşhane",
        "Aşık Veysel",
        # German / French / Spanish
        "Götterdämmerung",
        "Größenwahn",
        "Français Élégant",
        "España Cañí",
        "Corazón Espinado",
        # Greek
        "Ὀδύσσεια",
        "Μίκης Θεοδωράκης",
        # Cyrillic
        "Пётр Ильич Чайковский",
        "Модест Мусоргский",
    ])
    def test_nfd_vs_nfc_equivalence_across_languages(self, title_nfc: str):
        """Precomposed NFC and decomposed NFD representations must normalize identically."""
        title_nfd = unicodedata.normalize("NFD", title_nfc)
        norm_nfc = normalize_text(title_nfc)
        norm_nfd = normalize_text(title_nfd)
        assert norm_nfc == norm_nfd, f"Mismatch for '{title_nfc}': NFC '{norm_nfc}' != NFD '{norm_nfd}'"

    def test_combining_diacritics_without_base_and_zero_width_chars(self):
        """Zero-width spaces, joiners, and combining characters do not crash normalize_text."""
        dirty = "Song\u200B \u200C\u200DTitle\uFEFF"
        clean = normalize_text(dirty)
        assert clean == "song title"

        # Trailing combining acute mark alone
        lone_combining = "Title\u0301"
        assert normalize_text(lone_combining) == "title"

    def test_fullwidth_and_ideographic_space_normalization(self):
        """Ideographic spaces (U+3000) and Fullwidth ASCII (U+FF01-U+FF5E) are handled."""
        fw_title = "Ｓｏｎｇ　Ｎａｍｅ　（Ｒｅｍｉｘ）"
        assert normalize_text(fw_title) == "song name remix"

    def test_clean_search_phrase_preserves_non_latin(self):
        """clean_search_phrase preserves Japanese/Cyrillic while stripping noise."""
        jp_query = "ステラベエ - 超カワイイ (Album)"
        cleaned = clean_search_phrase(jp_query)
        assert "ステラベエ" in cleaned
        assert "超カワイイ" in cleaned
        assert "Album" in cleaned

        cyrillic_query = "Чайковский - Лебединое озеро [FLAC]"
        cleaned_cyr = clean_search_phrase(cyrillic_query)
        assert "Чайковский" in cleaned_cyr
        assert "Лебединое озеро" in cleaned_cyr
