"""Splitting MusicBrainz artist credits and matching them against peer artist names."""

import pytest

from discogseek.core.text import artist_credit_matches, primary_artist, split_artist_credit


@pytest.mark.parametrize("credit, names", [
    ("Lone", ("lone",)),
    ("Lone feat. Bikôkô", ("lone", "bikoko")),
    ("Bianca Oblivion ft. Lunice & Lamsi", ("bianca oblivion", "lunice", "lamsi")),
    ("Danny Daze, Tristan Arp", ("danny daze", "tristan arp")),
    ("Richard Spaven Feat. Feat.Jordan Rakei", ("richard spaven", "jordan rakei")),
    ("Zeb Samuels & & Kiko Continentino", ("zeb samuels", "kiko continentino")),
    ("Cayos + Nerikatsu", ("cayos", "nerikatsu")),
    ("Body Language x Machinedrum", ("body language", "machinedrum")),
    ("Vertex vs. Waveform", ("vertex", "waveform")),
    ("Simon and Garfunkel", ("simon", "garfunkel")),
    ("Machinedrum with Tanerélle", ("machinedrum", "tanerelle")),
    ("A; B / C", ("a", "b", "c")),
    ("The Yellow Moon Band", ("yellow moon band",)),
    ("And One", ("and one",)),
    ("X Ambassadors", ("x ambassadors",)),
    ("Malcolm X", ("malcolm x",)),
    ("AC/DC", ("ac dc",)),
    ("", ()),
])
def test_split_artist_credit(credit, names):
    assert split_artist_credit(credit) == names


@pytest.mark.parametrize("credit, primary", [
    ("Lone feat. Bikôkô", "Lone"),
    ("Danny Daze, Tristan Arp", "Danny Daze"),
    ("Steve & Rob", "Steve"),
    ("Machinedrum", "Machinedrum"),
    ("The Field", "The Field"),
])
def test_primary_artist_is_a_display_name(credit, primary):
    assert primary_artist(credit) == primary


@pytest.mark.parametrize("expected, remote", [
    ("Zeb Samuels & & Kiko Continentino", "Zeb Samuels"),
    ("Richard Spaven Feat. Feat.Jordan Rakei", "Richard Spaven"),
    ("BADBADNOTGOOD Feat. Charlotte Day Wilson", "BADBADNOTGOOD"),
    ("Lone feat. Bikôkô", "Lone feat. Bikoko"),
    ("Machinedrum feat. aja monet", "Machinedrum, aja monet"),
    ("μ‐Ziq", "u-Ziq"),
    ("μ-Ziq", "µ-Ziq"),
    ("µ-Ziq", "Mu-Ziq"),
    ("The Yellow Moon Band", "yellow moon band"),
    ("Machine Drum", "Machinedrum"),
    ("Sarah Farina & Yo van Lenz", "Sarah Farina Yo Van Lenz"),
    ("Cayos + Nerikatsu", "Audiose My Angel + Nerikatsu"),
    ("Florence and the Machine", "Florence + The Machine"),
    ("Machinedrum", "Machinedrum feat. Tanerélle"),
    ("Lone", "Lone (UK electronic artist Matt Cutler)"),
    ("Simon & Garfunkel", "Simon and Garfunkel"),
    ("Azealia Banks & Lone", "Azealia Banks"),
    ("The Prodigy", "Prodigy"),
])
def test_artist_credit_matches_peer_spellings(expected, remote):
    assert artist_credit_matches(expected, remote)


@pytest.mark.parametrize("expected, remote", [
    ("Lone", "Gold Panda"),
    ("Machinedrum", "Machine Girl"),
    # One shared word is another artist, also when it comes from splitting a band name.
    ("Lone", "Lone Wolf"),
    ("Iron & Wine", "Iron Maiden"),
    ("Florence + the Machine", "Rage Against the Machine"),
    ("Mumford & Sons", "Sons of Kemet"),
    ("Earth, Wind & Fire", "Arcade Fire"),
    ("Lone", ""),
    ("", "Lone"),
])
def test_artist_credit_rejects_other_artists(expected, remote):
    assert not artist_credit_matches(expected, remote)


def test_artist_credit_accepts_aliases():
    assert artist_credit_matches("Machinedrum", "Sepalcure", aliases=["Sepalcure", "JETS"])
    assert not artist_credit_matches("Machinedrum", "Sepalcure", aliases=["JETS"])
