"""Vinyl sides, 'DNN' prefixes, and disc folders in track position parsing."""

import pytest

from discogseek.core.release_metadata import (
    disc_number_from_folder,
    parse_disc_and_track_number,
    vinyl_position,
)


@pytest.mark.parametrize("value, position", [
    ("A1", ("A", 1)),
    ("b2", ("B", 2)),
    ("A01", ("A", 1)),
    ("D12", ("D", 12)),
    ("B", ("B", 1)),
    ("1", None),
    ("1-01", None),
    ("M83", None),
    ("U2", None),
    ("A0", None),
    ("", None),
    (None, None),
])
def test_vinyl_position(value, position):
    assert vinyl_position(value) == position


@pytest.mark.parametrize("name, disc", [
    ("CD 2", 2),
    ("CD2", 2),
    ("cd_02", 2),
    ("Disc 1 - Bonus Tracks", 1),
    ("CD1 (Remaster)", 1),
    ("CD2 Greatest Hits", 2),
    ("Disc One", 1),
    ("LP 2", 2),
    ("Album (Disc 2)", 2),
    ("Dreams [Disc 4]", 4),
    ("Half Gentlemen Not Beasts (disc 2)", 2),
    ("Album - CD 2", 2),
    ("Album_CD2 [FLAC]", 2),
    ("Swings & Roundabouts - The Very Best Of The Jetset [2010] CD1", 1),
    ("ohio players - the mercury anthology - disc 1 of 2", 1),
    ("Pressure the Hinges, Disc 1", 1),
    ("[ZEN CD 215] Machinedrum - Vapor City Archives", None),
    ("[WARP CD 92] Prefuse 73 - One Word Extinguisher", None),
    ("The Smiths [1986] {ROUGH CD61}", None),
    ("CD-2002", None),
    ("(CD 1998)", None),
    ("cd21 but now these days are gone", None),
    ("Robert Plant - Nine Lives (Box Set 9CD) (FLAC)", None),
    ("CD Baby", None),
    ("Discotheque", None),
    ("The Early Years of Italo Disco. 5", None),
    ("Side B", None),
    ("CD 0", None),
    ("", None),
    # A disc token in the middle of the name, followed by a subtitle (live peer folder names).
    ("1987 - The Joshua Tree CD3 Remixes", 3),
    ("Amy Winehouse - The Collection CD1 Frank (2020)", 1),
    ("Diamonds And Pearls (Remastered Super Deluxe Edition) - CD 4 - Unreleased Studio Record", 4),
    ("Global Communication - 76.14 remastered (CD 2 - singles and bonus tracks)", 2),
    ("Buddha-Bar VI (CD2 - Rejoice) (By Ravin) (2004)", 2),
    ("Kiss Mix 98 (CD2, Graham Gold)", 2),
    ("Donny Hathaway - Disc 2 - Unreleased Studio  Recordings", 2),
    ("Disc 1 - 1st Dream", 1),
    ("Artist-Album-CD1-FLAC-2010-GRP", 1),
    # Catalog numbers inside brackets, years, and folders that hold several discs.
    ("Artificial Intelligence [1992][WARP CD 6]", None),
    ("Aril Brikha - Deeparture In Time [Transmat (TMT-CD-6)] - 2000", None),
    ("Combichrist-The_Venom_In_The_Mouth_Of_God-CD-2026-FWYH", None),
    ("Gridlok-Void-P51CD04-BONUS_CD-2010-sour", None),
    ("VA-Album-2CD-2010-GRP", None),
    ("AA.VV. - Gentle Giant Tribute - cd 1 & 2 -", None),
    ("The Radio Essentials Serie  CD 01-60", None),
])
def test_disc_number_from_folder(name, disc):
    assert disc_number_from_folder(name) == disc


@pytest.mark.parametrize("raw_track, filename, meta_disc, expected", [
    # Artist names that start with a letter and digits are not vinyl sides.
    ("1", "M83 - Midnight City.flac", 1, (1, 1)),
    ("1", "U2 - One.mp3", 1, (1, 1)),
    ("1", "C418 - Sweden.flac", 1, (1, 1)),
    (None, "M83 - Midnight City.flac", None, (1, None)),
    (None, "U2 - One.mp3", None, (1, None)),
    (None, "E40 - Choices.mp3", None, (1, None)),
    (None, "B52s - Rock Lobster.mp3", None, (1, None)),
    # A filename prefix naming another position never overrides a numeric tag.
    ("3", "B1 - Song.flac", 1, (1, 3)),
    ("3", "2-01 Song.flac", 1, (1, 3)),
    # 'DDTT' prefixes; a leading year is no position.
    (None, "0101 - Coreshine Voodoo.flac", None, (1, 1)),
    (None, "0205 - Song.flac", None, (2, 5)),
    (None, "0018 - Carlos Puebla - Cambio.flac", None, (1, 18)),
    (None, "1999 - Song.flac", None, (1, None)),
])
def test_parse_disc_and_track_number_prefixes(raw_track, filename, meta_disc, expected):
    assert parse_disc_and_track_number(raw_track, filename=filename, meta_disc=meta_disc)[:2] == expected


@pytest.mark.parametrize("filename, disc_count, expected", [
    ("305 - Global Communication - 5 23 (Lone Remix).flac", 3, (3, 5)),
    ("306-mark_de_clive-lowe-restless.mp3", 3, (3, 6)),
    ("101 - Song.flac", 1, (1, 1)),
    ("305 - Song.flac", 2, (1, 305)),
    ("305 - Song.flac", None, (1, 305)),
    ("100 - Young Echo - Blood Sugar.mp3", 1, (1, 100)),
    ("023-machinedrum-diepure.mp3", 2, (1, 23)),
])
def test_three_digit_prefix_is_disc_and_track_within_the_disc_count(filename, disc_count, expected):
    assert parse_disc_and_track_number(None, filename=filename, disc_count=disc_count)[:2] == expected
