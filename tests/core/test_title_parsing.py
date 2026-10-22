"""Peer filename and MusicBrainz title parsing: positions, artist prefixes, noise tags, and versions."""

import unicodedata

import pytest

from discogseek.core.constants import POLISH_DIACRITICS_TABLE
from discogseek.core.text import (
    are_versions_compatible,
    clean_search_phrase,
    fold_search_phrase,
    normalize_text,
    parse_track_title_structure,
    strip_track_number_and_artist,
    title_match_score,
    title_variants,
)


class TestNormalization:
    def test_underscore_is_a_space(self):
        assert normalize_text("big_world") == "big world"
        assert normalize_text("Machine_Drum_-_Psyconia") == "machine drum psyconia"

    def test_ampersand_reads_as_and(self):
        assert normalize_text("Tried & True") == normalize_text("Tried and True") == "tried and true"

    @pytest.mark.parametrize("title", ["★", "∞", "☆ ★"])
    def test_symbol_titles_stay_comparable(self, title):
        assert normalize_text(title) == unicodedata.normalize("NFC", title).casefold()

    def test_emoji_variation_selectors_are_ignored(self):
        assert normalize_text("❤️") == normalize_text("❤") == "❤"

    def test_ascii_punctuation_titles_keep_their_symbols(self):
        assert normalize_text("???") == "???"
        assert normalize_text("]") == "]"

    def test_polish_table_has_code_point_keys(self):
        assert "Łódź Żółć".translate(POLISH_DIACRITICS_TABLE) == "Lodz Zolc"


class TestSearchPhrases:
    @pytest.mark.parametrize("query", ["नमस्ते दुनिया", "แม่ค้าส้มตำ", "Кино Группа крови"])
    def test_combining_marks_keep_words_whole(self, query):
        assert clean_search_phrase(query) == query

    def test_punctuation_and_dangling_separators_are_dropped(self):
        assert clean_search_phrase("Artist: Title!") == "Artist Title"
        assert clean_search_phrase("David Bowie - ★") == "David Bowie"
        assert clean_search_phrase("Machinedrum - U_WANT") == "Machinedrum - U_WANT"

    def test_folded_variant_reaches_ascii_named_shares(self):
        assert fold_search_phrase("Zażółć gęślą jaźń") == "Zazolc gesla jazn"
        assert fold_search_phrase("すてらべえ Café") == "すてらべえ Cafe"


class TestNoiseTags:
    @pytest.mark.parametrize("filename, base", [
        ("07 - Cody [FLAC] [889 kbps].flac", "cody"),
        ("07 - Cody [24bit 48kHz].flac", "cody"),
        ("07 - Cody (24-96).flac", "cody"),
        ("07 - Cody [16bit-44.1kHz].flac", "cody"),
        ("07 - Cody (320kbps).flac", "cody"),
        ("03 Song [16-44].flac", "song"),
        ("01 - Koran Angel [FLAC] [754 kbps].flac", "koran angel"),
        ("03 - Cambio [FLAC 16bit - 961 kbps - 44.1kHz].flac", "cambio"),
        ("05 - Title Song (Explicit).flac", "title song"),
        ("05 - Title Song [Clean].flac", "title song"),
        ("05 - Big World - 2012 Remaster.flac", "big world"),
        ("05 - Big World (2009 Remaster).flac", "big world"),
        ("05 - Big World - Remastered 2011.flac", "big world"),
        ("11 Epiphany (2006).mp3", "epiphany"),
        ("01 Title.aif", "title"),
        ("01-01 Rollercoaster.FLAC.932 kbps.flac", "rollercoaster"),
        ("10 Feet In The Clouds  320bps.mp3", "feet in the clouds"),
        ("02 Title (Official Audio).mp3", "title"),
        ("09 Work Eat Sleep [Pirate-Punk.net].mp3", "work eat sleep"),
        ("D. Diggler - Memory Man (Original Mix) [ClapCrate.net].mp3", "memory man"),
        ("61 Mo Kolours - Biddies - 7A - 96.4.flac", "biddies"),
        ("03 Africa HiTech - Out in the Streets - 2A - 80.0_PN_PN.mp3", "out in the streets"),
        ("07 - Yo What Uh Yeh [Japanese Bonus Track].flac", "yo what uh yeh"),
        ("05 - Witches [V0].mp3", "witches"),
        ("05 - Witches [MP3 V2].mp3", "witches"),
    ])
    def test_noise_is_stripped_from_peer_filenames(self, filename, base):
        parsed = parse_track_title_structure(filename)
        assert parsed["base_norm"] == base
        assert parsed["version_type"] is None

    @pytest.mark.parametrize("filename", [
        "05 - Witches v2.flac", "05 - Witches (v2).flac", "05 - Witches - V2.flac",
    ])
    def test_second_version_marker_is_not_a_quality_tag(self, filename):
        # 'Witches' and 'Witches v2' can share a release; the marker must keep them apart.
        assert parse_track_title_structure(filename)["base_norm"] == "witches v2"
        assert parse_track_title_structure("Witches v2", is_filename=False)["base_norm"] == "witches v2"

    @pytest.mark.parametrize("title", [
        "Drip", "Trip", "Wave", "Heatwave Blues", "Comic Strip", "Cobweb", "Eurocore MVP",
        "Airwaves", "Electric Waver", "Lyrics", "Vinyl", "Rip", "Web", "CD", "Cdrom",
    ])
    def test_format_words_inside_titles_survive(self, title):
        assert parse_track_title_structure(title)["base_norm"] == normalize_text(title)
        assert parse_track_title_structure(f"04 - {title}.flac")["base_norm"] == normalize_text(title)

    def test_noise_only_title_is_kept(self):
        assert parse_track_title_structure("(Lyrics)", is_filename=False)["base_norm"] == "lyrics"


class TestFeatures:
    @pytest.mark.parametrize("title", ["Feathers", "FTA", "Feats of Strength", "Charcoal Feather",
                                       "Ballad of Gloria Featherbottom"])
    def test_feature_keyword_needs_a_word_boundary(self, title):
        parsed = parse_track_title_structure(title, is_filename=False)
        assert parsed["base_norm"] == normalize_text(title)
        assert parsed["features"] == []

    def test_unbracketed_feature_keeps_hyphenated_names(self):
        parsed = parse_track_title_structure("Song ft. Jay-Z", is_filename=False)
        assert parsed["base_norm"] == "song"
        assert parsed["features"] == ["jay z"]


class TestVersions:
    @pytest.mark.parametrize("title", [
        "Title Song (feat. Oliver Tree)", "Title Song (feat. Clive)", "Title Song (feat. Edith Frost)",
        "Title Song (feat. DEMONDICE)", "Title Song (Deluxe Edition)",
        "Title Song (Original Mix)", "Title Song (with Dubstep Dan)",
    ])
    def test_credits_and_editions_are_not_versions(self, title):
        parsed = parse_track_title_structure(title, is_filename=False)
        assert parsed["base_norm"] == "title song"
        assert parsed["version_type"] is None

    @pytest.mark.parametrize("title", ["Title Song (Credits)", "Title Song (Meredith)", "Title Song (Flipside)"])
    def test_words_containing_version_keywords_are_not_versions(self, title):
        assert parse_track_title_structure(title, is_filename=False)["version_type"] is None

    @pytest.mark.parametrize("title, version_type", [
        ("Song (Live)", "live"),
        ("Song (Acid Techno Uziflip)", "remix"),
        ("Song (RINEDIT)", "remix"),
        ("Song (Remixed by Bob)", "remix"),
        ("Song (Demo)", "demo"),
        ("Song (Dub)", "mix_edit"),
        ("Song (Jimpster mix)", "remix"),
        ("Song (Extended Mix)", "mix_edit"),
        ("Song (VIP Mix)", "vip"),
        ("Gv (rmx)", "remix"),
    ])
    def test_version_keywords_are_whole_words(self, title, version_type):
        assert parse_track_title_structure(title, is_filename=False)["version_type"] == version_type

    def test_named_mix_is_the_same_family_as_named_remix(self):
        mix = parse_track_title_structure("Nepalese Bliss (Jimpster mix)", is_filename=False)
        remix = parse_track_title_structure("Nepalese Bliss (Jimpster Remix)", is_filename=False)
        other = parse_track_title_structure("Nepalese Bliss (Four Tet remix)", is_filename=False)
        assert mix["base_norm"] == remix["base_norm"] == "nepalese bliss"
        assert are_versions_compatible(mix["version_type"], mix["version_text"], remix["version_type"], remix["version_text"])
        assert are_versions_compatible("remix", "four tet mix", "remix", "four tet remix")
        assert not are_versions_compatible(mix["version_type"], mix["version_text"], other["version_type"], other["version_text"])

    def test_alternate_and_alternative_mix_are_one_version(self):
        first = parse_track_title_structure("Brainfreeze (alternate mix)", is_filename=False)
        second = parse_track_title_structure("01-07 - Brainfreeze (alternative mix).flac")
        assert first["version_type"] == second["version_type"] == "demo"
        assert are_versions_compatible(
            first["version_type"], first["version_text"], second["version_type"], second["version_text"],
        )

    @pytest.mark.parametrize("title, base, version_type", [
        ("Skelechairs Remix", "skelechairs", "remix"),
        ("Flycatcha VIP", "flycatcha", "vip"),
        ("Song Radio Edit", "song", "remix"),
        ("Song Original Mix", "song", None),
        ("Song Remastered 2011", "song", None),
    ])
    def test_trailing_unbracketed_version_words(self, title, base, version_type):
        parsed = parse_track_title_structure(title, is_filename=False)
        assert (parsed["base_norm"], parsed["version_type"]) == (base, version_type)


class TestFilenameTitles:
    @pytest.mark.parametrize("filename, base", [
        # Scene names 'NN-artist-title(-group)' and underscore separators
        ("05-lone-big_world.mp3", "big world"),
        ("01_-_Lone_-_Big_World.mp3", "big world"),
        ("01_Lone_-_Coreshine_Voodoo.mp3", "coreshine voodoo"),
        ("01_machine_drum_-_only_one.flac", "only one"),
        ("04-lone--visited_by_astronauts-oma.mp3", "visited by astronauts"),
        ("09-cadence_weapon-bots-ftd.mp3", "bots"),
        ("02_mitiska_with_profetik_ft_julie_thompson-blue_(nitrous_oxide_club_remix).mp3", "blue"),
        ("Gold_Panda-You.mp3", "you"),
        ("03-Re-Up.flac", "re up"),
        # Positions: disc-track, vinyl sides, 'DDTT', bracketed numbers
        ("Artist - Album - 03 - Title.flac", "title"),
        ("A1 - Title.flac", "title"),
        ("B2. Title.flac", "title"),
        ("a1 Title.flac", "title"),
        ("0101 - Coreshine Voodoo.flac", "coreshine voodoo"),
        ("1 - 03. I Had a Memory.mp3", "i had a memory"),
        ("05 2-05 Now You Know (Part 1).flac", "now you know part 1"),
        ("(02) Title.mp3", "title"),
        ("Lone - Hyperphantasia - 14 Home (feat. Merry Lamb Lamb).flac", "home"),
        ("18 - Robert Schumann - Kinderszenen, Op.15 - 11. Fürchtenmachen.flac", "furchtenmachen"),
        # Numbers that belong to the title
        ("05 2 Is 8.flac", "2 is 8"),
        ("05 - 2 Is 8.flac", "2 is 8"),
        ("20 - Global Communication - 5 23 (Lone Remix).flac", "5 23"),
        ("03 - 7.39 (Original Cassette Demo).flac", "7 39"),
        ("05 - 9 (Autechre Remix).flac", "9"),
        ("01 - 1999.flac", "1999"),
        ("23 22 8.mp3", "22 8"),
        ("01 10 Watt Lazers.mp3", "10 watt lazers"),
        # Colon substitutes and decorations
        ("03 - Interlude： Night Drive.flac", "interlude night drive"),
        ("01 - ★.flac", "★"),
        ("02 (mind apple intro).flac", "mind apple intro"),
    ])
    def test_filename_title(self, filename, base):
        assert parse_track_title_structure(filename)["base_norm"] == base

    def test_scene_feature_and_group_tags(self):
        parsed = parse_track_title_structure("01-machinedrum-only_one_(feat._angelica_bess).flac")
        assert parsed["base_norm"] == "only one"
        assert parsed["features"] == ["angelica bess"]
        tagged = parse_track_title_structure("Mark_Sixma__Domeno-Lose_Control-Extended_Mix-54197181.mp3")
        assert (tagged["base_norm"], tagged["version_type"]) == ("lose control", "mix_edit")

    @pytest.mark.parametrize("filename, base, version_type", [
        # A trailing version segment is not a scene group tag, and never folds into the artist.
        ("01-artist-song-remix.mp3", "song", "remix"),
        ("01-artist-song-live.mp3", "song", "live"),
        ("01-artist-song-remix-ftd.mp3", "song", "remix"),
        ("03-machine_drum-izey_rael__icy_rail_remix-pulse.mp3", "izey rael icy rail", "remix"),
        ("15-Cinelux-Hollis_Machine_Drum_remix.mp3", "hollis machine drum", "remix"),
        ("01-song-remix.mp3", "song", "remix"),
    ])
    def test_scene_version_segments(self, filename, base, version_type):
        parsed = parse_track_title_structure(filename)
        assert (parsed["base_norm"], parsed["version_type"]) == (base, version_type)

    def test_tilde_and_dash_wrapped_versions_are_brackets(self):
        ending = parse_track_title_structure("Ending ~TV size~.flac")
        opening = parse_track_title_structure("Opening ~TV size~", is_filename=False)
        assert (ending["base_norm"], ending["version_type"]) == ("ending", "mix_edit")
        assert opening["base_norm"] == "opening"
        assert parse_track_title_structure("Lose Control -Extended Mix-.flac")["base_norm"] == "lose control"
        assert strip_track_number_and_artist("01 ~ Artist Name ~ Clean Title") == "Clean Title"

    def test_only_the_real_extension_is_stripped(self):
        assert parse_track_title_structure("07 - YASTM.m4a.mp3")["base_norm"] == "yastm m4a"
        assert parse_track_title_structure("YASTM.m4a", is_filename=False)["base_norm"] == "yastm m4a"
        assert parse_track_title_structure("BRKSNG.IT(RMX)", is_filename=False)["base_norm"] == "brksng it"

    def test_strip_keeps_display_title(self):
        assert strip_track_number_and_artist("05-lone-big_world") == "big world"
        assert strip_track_number_and_artist("05 2 Is 8") == "2 Is 8"
        assert strip_track_number_and_artist("Machinedrum - Mergerz & Acquisitionz Sampler - 03 - Da Kidness Mix") == (
            "Da Kidness Mix"
        )


class TestTitleMode:
    @pytest.mark.parametrize("title, base", [
        ("2 Is 8", "2 is 8"),
        ("7 39 (original cassette demo)", "7 39"),
        ("5:23 (Lone remix)", "5 23"),
        ("1999", "1999"),
        ("Let Me In - Cosmic Orchestra Ver.", "let me in cosmic orchestra ver"),
        ("Natural Aerials - Mouth of God Part Two", "natural aerials mouth of god part two"),
        ("Interlude: Night Drive", "interlude night drive"),
        ("YASTM.m4a", "yastm m4a"),
        ("A1 Steppa", "a1 steppa"),
    ])
    def test_titles_are_never_cut(self, title, base):
        assert parse_track_title_structure(title, is_filename=False)["base_norm"] == base

    def test_titles_sharing_a_suffix_stay_distinct(self):
        first = parse_track_title_structure("Mouth of God - Intro", is_filename=False)
        second = parse_track_title_structure("Piano and Wings - Intro", is_filename=False)
        assert first["base_norm"] != second["base_norm"]

    def test_trailing_version_segment_becomes_the_version(self):
        parsed = parse_track_title_structure("Rengoku - Deru Remix", is_filename=False)
        assert (parsed["base_norm"], parsed["version_type"]) == ("rengoku", "remix")


class TestTitleVariants:
    def test_filename_variants_include_every_dash_suffix(self):
        variants = title_variants("03 - Interlude - Night Drive.flac")
        assert variants[0] == "Night Drive"
        assert "Interlude - Night Drive" in variants

    def test_ambiguous_leading_number_is_kept_as_a_variant(self):
        assert title_variants("2 Is 8.flac") == ("Is 8", "2 Is 8")

    def test_extension_like_endings(self):
        assert title_variants("YASTM.m4a", is_filename=False) == ("YASTM.m4a", "YASTM")
        assert title_variants("07 - YASTM.m4a.mp3") == ("YASTM.m4a", "YASTM")


class TestTitleMatchScore:
    @pytest.mark.parametrize("expected, filename", [
        ("2 Is 8", "05 2 Is 8.flac"),
        ("2 Is 8", "05 - 2 Is 8.flac"),
        ("2 Is 8", "2 Is 8.flac"),
        ("22/8", "23 22 8.mp3"),
        ("9 (Autechre remix)", "05 - 9 (Autechre Remix).flac"),
        ("5:23 (Lone remix)", "20 - Global Communication - 5 23 (Lone Remix).flac"),
        ("5:23 (Lone remix)", "305 - Global Communication - 5：23 Lone Remix.flac"),
        ("Gunshotta (Lone remix)", "01 - Machinedrum - Gunshotta Lone Remix.flac"),
        ("Interlude: Night Drive", "03 - Interlude - Night Drive.flac"),
        ("Interlude: Night Drive", "03 - Interlude： Night Drive.flac"),
        ("Mouth of God - Intro", "05 Artist - Mouth of God - Intro.flac"),
        ("Let Me In - Cosmic Orchestra Ver.", "04 Let Me In (Cosmic Orchestra Ver.).flac"),
        ("YASTM.m4a", "07 - YASTM.m4a.mp3"),
        ("YASTM.m4a", "07-cadence_weapon-yastm_m4a-ftd.mp3"),
        ("★", "01 - ★.flac"),
        ("Title Song", "07 - Title Song (feat. Oliver Tree).flac"),
        ("Affinity (Cloud Four Four mix)", "0103 - Affinity Cloud Four Four mix.flac"),
        ("Nepalese Bliss (Jimpster mix)", "03 - Nepalese Bliss (Jimpster Remix).flac"),
        ("Skelechairs (Venetian Snares remix)", "Doormouse - Skelechairs Remix.flac"),
        ("Big World", "Lone-Emerald_Fantasy_Tracks-WEB-2012-FTD/05-lone-big_world.mp3".split("/")[-1]),
        ("Tried and True", "04 - Tried & True.flac"),
        # A zero-padded number that begins the title, after a track number or alone.
        ("007 (Shanty Town)", "05 - 007 (Shanty Town).flac"),
        ("007 (Shanty Town)", "007 (Shanty Town).flac"),
        ("00:00", "01 - 00 00.flac"),
        ("05:00 AM", "03 - 05.00 AM.mp3"),
    ])
    def test_same_recordings_match(self, expected, filename):
        assert title_match_score(expected, filename) >= 0.85

    @pytest.mark.parametrize("expected, filename", [
        ("4 Year Coma", "3 Year Coma.flac"),
        ("4 Year Coma", "03 3 Year Coma.flac"),
        ("4 Year Coma", "05 5 Year Coma.flac"),
        ("Opening ~TV size~", "Ending ~TV size~.flac"),
        ("Gunshotta", "01 - Machinedrum - Gunshotta Lone Remix.flac"),
        ("Mouth of God - Intro", "02 Artist - Piano and Wings - Intro.flac"),
        ("Nepalese Bliss (Jimpster mix)", "03 - Nepalese Bliss (Four Tet Remix).flac"),
        ("A Much Longer Title Than Usual Here (Live)", "05 - A Much Longer Title Than Usual Here.flac"),
        ("A Much Longer Title Than Usual Here", "05 - A Much Longer Title Than Usual Here (Live).flac"),
        ("Cob", "05 - Cobweb.flac"),
        ("Drip", "03 - I'd Rather.flac"),
        ("Track 4", "04 Track 3.flac"),
    ])
    def test_different_recordings_do_not_match(self, expected, filename):
        assert title_match_score(expected, filename) < 0.80
