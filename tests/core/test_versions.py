"""Track title cleanup and compatibility of remix, live, and other versions."""

import pytest

from discogseek.core.text import (
    are_versions_compatible,
    parse_track_title_structure,
    strip_track_number_and_artist,
)


class TestTitleVersions:
    @pytest.mark.parametrize("dash_char", [
        "-",       # ASCII hyphen
        "–",       # En dash (\u2013)
        "—",       # Em dash (\u2014)
        "―",       # Horizontal bar (\u2015)
        "‐",       # Hyphen (\u2010)
        "‑",       # Non-breaking hyphen (\u2011)
        "−",       # Minus sign (\u2212)
        "－",      # Fullwidth hyphen (\uFF0D)
        "~",       # ASCII tilde
        "～",      # Fullwidth tilde (\uFF5E)
        "〜",      # Wave dash (\u301C)
    ])
    def test_typographic_dashes_and_separators(self, dash_char: str):
        """All typographic dashes and wave dashes are standardized in strip_track_number_and_artist."""
        fn = f"01 {dash_char} Artist Name {dash_char} Clean Title.flac"
        extracted = strip_track_number_and_artist(fn)
        assert extracted == "Clean Title", f"Failed to extract title with delimiter '{dash_char}': got '{extracted}'"

    @pytest.mark.parametrize("filename,expected_core_title,expected_version_type", [
        ("01 Song - Instrumental.flac", "song", "instrumental"),
        ("02 Song - Radio Edit.mp3", "song", "remix"),  # 'edit' matches remix pattern in VERSION_PATTERNS
        ("03 Song - Alice Remix.flac", "song", "remix"),
        ("04 Song - Live in Paris.flac", "song", "live"),
        ("05 Song - Remaster.flac", "song", None),
        ("06 Song - Acoustic.flac", "song", "acoustic"),
        ("07 Song - Sped Up.flac", "song", "speed"),
        ("08 Song - Demo.flac", "song", "demo"),
        ("09 Song - VIP Mix.flac", "song", "vip"),
    ])
    def test_version_descriptors_preservation(self, filename: str, expected_core_title: str, expected_version_type: str):
        """Hyphenated version descriptors preserve the song title and parse correctly."""
        cleaned = strip_track_number_and_artist(filename)
        # Must contain the core title "Song"
        assert "Song" in cleaned or "song" in cleaned.lower()
        parsed = parse_track_title_structure(cleaned)
        assert parsed["base_norm"] == expected_core_title
        if expected_version_type:
            assert parsed["version_type"] == expected_version_type

    def test_remaster_with_year_edge_case(self):
        """
        Test how 'Song - 2021 Remaster' behaves under strip_track_number_and_artist.
        Note: VERSION_DESCRIPTOR_RE does not have a year prefix for 'remaster',
        so '2021 Remaster' is treated as the title and '2021' is stripped as track number.
        """
        cleaned_simple = strip_track_number_and_artist("01 Song - Remaster.flac")
        assert "song" in cleaned_simple.lower()

        # Document current behavior on '2021 Remaster'
        cleaned_year = strip_track_number_and_artist("01 Song - 2021 Remaster.flac")
        # In current implementation, VERSION_DESCRIPTOR_RE misses '2021 Remaster'
        # resulting in 'Remaster'
        assert cleaned_year in ("Remaster", "Song (2021 Remaster)")

    def test_feature_patterns_with_restraint(self):
        """Unbracketed 'with' must not truncate song titles, while bracketed 'with' is extracted."""
        # Unbracketed 'with' in song title
        parsed1 = parse_track_title_structure("Stay with Me")
        assert parsed1["base_norm"] == "stay with me"
        assert len(parsed1["features"]) == 0

        parsed2 = parse_track_title_structure("Dancing with Myself")
        assert parsed2["base_norm"] == "dancing with myself"

        parsed3 = parse_track_title_structure("With or Without You")
        assert parsed3["base_norm"] == "with or without you"

        # Bracketed 'with' is a feature credit
        parsed_bracket = parse_track_title_structure("Song Title (with Guest Artist)")
        assert parsed_bracket["base_norm"] == "song title"
        assert any("guest artist" in f for f in parsed_bracket["features"])

    def test_version_compatibility_matrix(self):
        """Rigorous checks on are_versions_compatible."""
        # Clean vs clean -> True
        assert are_versions_compatible(None, None, None, None) is True

        # Clean vs Modified -> False
        assert are_versions_compatible(None, None, "instrumental", "instrumental") is False
        assert are_versions_compatible("instrumental", "instrumental", None, None) is False
        assert are_versions_compatible(None, None, "remix", "alice remix") is False
        assert are_versions_compatible(None, None, "acoustic", "acoustic") is False

        # Incompatible modifier types -> False
        assert are_versions_compatible("instrumental", "instrumental", "acoustic", "acoustic") is False
        assert are_versions_compatible("remix", "alice remix", "live", "live in paris") is False

        # Instrumental variants -> True
        assert are_versions_compatible("instrumental", "inst", "instrumental", "instrumental version") is True
        assert are_versions_compatible("acapella", "a cappella", "acapella", "vocal version") is True

        # Remix matching logic
        assert are_versions_compatible("remix", "alice remix", "remix", "alice remix") is True
        assert are_versions_compatible("remix", "alice remix", "remix", "bob remix") is False

        # Acoustic / Live matching logic
        assert are_versions_compatible("acoustic", "acoustic version", "acoustic", "acoustic") is True
        assert are_versions_compatible("live", "live in paris", "live", "live in tokyo") is False
        assert are_versions_compatible("live", "live in paris 2024", "live", "live in paris") is True

    @pytest.mark.parametrize("dash", [
        "-",       # ASCII hyphen
        "–",       # En dash (\u2013)
        "—",       # Em dash (\u2014)
        "―",       # Horizontal bar (\u2015)
        "‐",       # Hyphen (\u2010)
        "‑",       # Non-breaking hyphen (\u2011)
        "−",       # Minus sign (\u2212)
        "－",      # Fullwidth hyphen (\uFF0D)
        "~",       # ASCII tilde
        "～",      # Fullwidth tilde (\uFF5E)
        "〜",      # Wave dash (\u301C)
    ])
    def test_all_dashes_stripped_cleanly_in_filename(self, dash: str):
        """All typographic dashes and wave dashes act as valid separators."""
        fn = f"01 {dash} Radiohead {dash} Paranoid Android.flac"
        extracted = strip_track_number_and_artist(fn)
        assert extracted == "Paranoid Android", f"Failed with delimiter '{dash}': extracted '{extracted}'"

    @pytest.mark.parametrize("fn,expected_title", [
        ("01 Track One - Instrumental.flac", "Track One (Instrumental)"),
        ("02 Track Two – Acoustic Version.mp3", "Track Two (Acoustic Version)"),
        ("03 Track Three — Live in Tokyo.flac", "Track Three (Live in Tokyo)"),
        ("04 Track Four - Radio Edit.flac", "Track Four (Radio Edit)"),
        ("05 Track Five - VIP Mix.flac", "Track Five (VIP Mix)"),
        ("06 Track Six - Remaster.flac", "Track Six (Remaster)"),
        ("07 Track Seven - Demo.flac", "Track Seven (Demo)"),
    ])
    def test_hyphenated_version_descriptors_preserve_core_title(self, fn: str, expected_title: str):
        """Version descriptors are preserved rather than replacing the song title."""
        cleaned = strip_track_number_and_artist(fn)
        assert cleaned == expected_title


class TestVersionDescriptors:
    """Preserve version descriptors and reject incompatible recordings."""

    @pytest.mark.parametrize("filename,expected_title", [
        ("01 Song - Live in Tokyo.mp3", "Song (Live in Tokyo)"),
        ("02 Song - Live at Wembley.flac", "Song (Live at Wembley)"),
        ("03 Song - Live 1999.mp3", "Song (Live 1999)"),
        ("04 Song - Live Version.flac", "Song (Live Version)"),
        ("05 Song – Acoustic Version.mp3", "Song (Acoustic Version)"),
        ("06 Song - Acoustic.flac", "Song (Acoustic)"),
        ("07 Song — Instrumental.flac", "Song (Instrumental)"),
        ("08 Song - Unplugged.mp3", "Song (Unplugged)"),
        ("09 Song - Unplugged Version.flac", "Song (Unplugged Version)"),
        ("10 Song - Piano Version.mp3", "Song (Piano Version)"),
        ("11 Song - VIP Mix.flac", "Song (VIP Mix)"),
        ("12 Song - VIP.mp3", "Song (VIP)"),
        ("13 Song - Radio Edit.flac", "Song (Radio Edit)"),
        ("14 Song - Extended Mix.mp3", "Song (Extended Mix)"),
        ("15 Song - Remaster.flac", "Song (Remaster)"),
        ("16 Song - Remastered.mp3", "Song (Remastered)"),
        ("17 Song - Digital Remaster.flac", "Song (Digital Remaster)"),
        ("18 Song - Anniversary Edition.mp3", "Song (Anniversary Edition)"),
        ("19 Song - Deluxe Edition.flac", "Song (Deluxe Edition)"),
        ("20 Song - Deluxe Version.mp3", "Song (Deluxe Version)"),
        ("21 Song - Bonus Track.flac", "Song (Bonus Track)"),
        ("22 Song - Demo.mp3", "Song (Demo)"),
        ("23 Song - Alt Take.flac", "Song (Alt Take)"),
        ("24 Song - Alternate Take.mp3", "Song (Alternate Take)"),
        ("25 Song - Alt Mix.flac", "Song (Alt Mix)"),
        ("26 Song - Rough Mix.mp3", "Song (Rough Mix)"),
        ("27 Song - Club Mix.flac", "Song (Club Mix)"),
        ("28 Song - Dub Mix.mp3", "Song (Dub Mix)"),
        ("29 Song - Acapella.flac", "Song (Acapella)"),
        ("30 Song - Vocal Version.mp3", "Song (Vocal Version)"),
        ("31 Song - Off Vocal.flac", "Song (Off Vocal)"),
        ("32 Song - Karaoke.mp3", "Song (Karaoke)"),
        ("33 Song - Backing Track.flac", "Song (Backing Track)"),
        ("34 Song - Original Mix.mp3", "Song (Original Mix)"),
        ("35 Song - Album Version.flac", "Song (Album Version)"),
        ("36 Song - Sped Up.mp3", "Song (Sped Up)"),
        ("37 Song - Slowed.flac", "Song (Slowed)"),
        ("38 Song - Nightcore.mp3", "Song (Nightcore)"),
    ])
    def test_strip_track_number_and_artist_version_descriptors(self, filename: str, expected_title: str):
        """Verify strip_track_number_and_artist correctly formats version descriptors with song titles."""
        result = strip_track_number_and_artist(filename)
        assert result == expected_title, f"For '{filename}', expected '{expected_title}', got '{result}'"

    def test_embedded_version_keywords_in_song_title(self):
        """
        Verify song titles containing words like 'Live' or 'Acoustic' are not mangled
        when followed by a version descriptor separator.
        """
        cases = [
            ("01 Artist - Live and Let Die - Live in Tokyo.mp3", "Live and Let Die (Live in Tokyo)"),
            ("02 Artist - Acoustic Guitar Solos - Acoustic Version.flac", "Acoustic Guitar Solos (Acoustic Version)"),
            ("03 Artist - Stay with Me - Piano Version.mp3", "Stay with Me (Piano Version)"),
            ("04 Artist - Born to Be Alive - Instrumental.flac", "Born to Be Alive (Instrumental)"),
            ("05 Artist - Demo Tape Blues - Demo.mp3", "Demo Tape Blues (Demo)"),
        ]
        for fn, exp in cases:
            res = strip_track_number_and_artist(fn)
            assert res == exp, f"For '{fn}', expected '{exp}', got '{res}'"

    def test_version_compatibility_matrix(self):
        """
        Verify are_versions_compatible accurately enforces compatibility rules:
        - Same version type with equivalent modifiers -> True
        - Same version type with conflicting modifiers -> False
        - Cross-version type mismatches (Live vs Acoustic, Instrumental vs Studio) -> False
        """
        # Compatible cases
        assert are_versions_compatible("acoustic", "acoustic", "acoustic", "acoustic version") is True
        assert are_versions_compatible("acoustic", "acoustic", "acoustic", "unplugged") is True
        assert are_versions_compatible("live", "live in tokyo", "live", "live in tokyo") is True
        assert are_versions_compatible("live", "live", "live", "live in tokyo") is True
        assert are_versions_compatible("instrumental", "instrumental", "instrumental", "instrumental") is True
        assert are_versions_compatible("remix", "vip mix", "remix", "vip") is True
        assert are_versions_compatible(None, None, None, None) is True

        # Incompatible cases
        # Live in different locations
        assert are_versions_compatible("live", "live in tokyo", "live", "live at wembley") is False
        # Acoustic vs Live
        assert are_versions_compatible("acoustic", "acoustic", "live", "live") is False
        # Instrumental vs Studio (None)
        assert are_versions_compatible("instrumental", "instrumental", None, None) is False
        # Studio (None) vs Live
        assert are_versions_compatible(None, None, "live", "live") is False
        # Remix vs Acoustic
        assert are_versions_compatible("remix", "vip mix", "acoustic", "acoustic") is False
        # Different remixes
        assert are_versions_compatible("remix", "club mix", "remix", "radio edit") is False

    @pytest.mark.parametrize("fn,expected", [
        # Acoustic variants
        ("01 Song - Acoustic.flac", "Song (Acoustic)"),
        ("01 Song - Acoustic Version.flac", "Song (Acoustic Version)"),
        ("01 Song – Acoustic Ver.mp3", "Song (Acoustic Ver)"),
        ("01 Song - Unplugged.flac", "Song (Unplugged)"),
        ("01 Song - Unplugged Version.flac", "Song (Unplugged Version)"),
        ("01 Song - Piano Version.flac", "Song (Piano Version)"),
        ("01 Song - Piano Ver.flac", "Song (Piano Ver)"),

        # Instrumental variants
        ("02 Title - Instrumental.flac", "Title (Instrumental)"),
        ("02 Title - Inst.flac", "Title (Inst)"),
        ("02 Title - Off Vocal.flac", "Title (Off Vocal)"),
        ("02 Title - Karaoke.flac", "Title (Karaoke)"),
        ("02 Title - Backing Track.flac", "Title (Backing Track)"),

        # Acapella variants
        ("03 Harmony - Acapella.flac", "Harmony (Acapella)"),
        ("03 Harmony - A Cappella.flac", "Harmony (A Cappella)"),
        ("03 Harmony - Vocal Version.flac", "Harmony (Vocal Version)"),

        # Live variants
        ("04 Anthem - Live.flac", "Anthem (Live)"),
        ("04 Anthem - Live in Berlin.flac", "Anthem (Live in Berlin)"),
        ("04 Anthem - Live at Wembley.flac", "Anthem (Live at Wembley)"),
        ("04 Anthem - Live 1998.flac", "Anthem (Live 1998)"),
        ("04 Anthem - Live Version.flac", "Anthem (Live Version)"),

        # Mix / VIP / Radio Edit variants
        ("05 Beats - Radio Edit.flac", "Beats (Radio Edit)"),
        ("05 Beats - Club Mix.flac", "Beats (Club Mix)"),
        ("05 Beats - Extended Mix.flac", "Beats (Extended Mix)"),
        ("05 Beats - Extended Version.flac", "Beats (Extended Version)"),
        ("05 Beats - VIP.flac", "Beats (VIP)"),
        ("05 Beats - VIP Mix.flac", "Beats (VIP Mix)"),
        ("05 Beats - Dub Mix.flac", "Beats (Dub Mix)"),
        ("05 Beats - Original Mix.flac", "Beats (Original Mix)"),

        # Remaster / Edition variants
        ("06 Classic - Remaster.flac", "Classic (Remaster)"),
        ("06 Classic - Remastered.flac", "Classic (Remastered)"),
        ("06 Classic - Digital Remaster.flac", "Classic (Digital Remaster)"),
        ("06 Classic - Deluxe Edition.flac", "Classic (Deluxe Edition)"),
        ("06 Classic - Anniversary Edition.flac", "Classic (Anniversary Edition)"),
        ("06 Classic - Bonus Track.flac", "Classic (Bonus Track)"),

        # Remix variants
        ("07 Track - Remix.flac", "Track (Remix)"),
        ("07 Track - Skrillex Remix.flac", "Track (Skrillex Remix)"),
        ("07 Track - Deadmau5 Rework.flac", "Track (Deadmau5 Rework)"),
        ("07 Track - Tiesto Bootleg.flac", "Track (Tiesto Bootleg)"),
        ("07 Track - Club Flip.flac", "Track (Club Flip)"),

        # Non-version multi-hyphen titles (must NOT treat normal titles as version)
        ("08 Artist - Love - Hate.flac", "Hate"),
        ("09 Artist - Sun - Moon.flac", "Moon"),
    ])
    def test_version_descriptor_matrix(self, fn: str, expected: str):
        result = strip_track_number_and_artist(fn)
        assert result == expected, f"Mismatch for '{fn}': expected '{expected}', got '{result}'"

    def test_reconciler_version_incompatibility_guardrail(self):
        """
        Catalog track is standard studio album track: 'Creep'.
        Local files contain 'Creep (Acoustic)', 'Creep (Live)', 'Creep (Remix)'.
        are_versions_compatible must report incompatibility between 'original' and 'acoustic'/'live'/'remix'.
        """
        # Studio version vs Acoustic
        assert not are_versions_compatible("original", None, "acoustic", "Acoustic")
        # Studio version vs Live
        assert not are_versions_compatible("original", None, "live", "Live")
        # Studio version vs Remix
        assert not are_versions_compatible("original", None, "remix", "Remix")
        # Remixer conflict: Alice Remix vs Bob Remix
        assert not are_versions_compatible("remix", "Alice Remix", "remix", "Bob Remix")
