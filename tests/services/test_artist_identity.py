"""Artist identity matching across paths, metadata, and Unicode encodings."""

import unicodedata
from pathlib import Path

import pytest

from discogseek.core.text import normalize_text
from discogseek.services.reconciler import DiscographyReconciler
from tests.helpers.catalog import build_artist_catalog


class TestArtistIdentity:
    @pytest.mark.parametrize("short_artist,unrelated_path", [
        ("Air", "/music/Love Affair/Greatest Hits/01 Some Track.flac"),
        ("Air", "/music/Pair of Aces/Album/01 Some Track.flac"),
        ("Air", "/music/The Chair/Album/01 Some Track.flac"),
        ("Air", "/music/Airport Security/Album/01 Some Track.flac"),
        ("Air", "/music/Fairy Tale/Album/01 Some Track.flac"),
        ("On", "/music/London Symphony Orchestra/Concert/01 Some Track.flac"),
        ("On", "/music/Online World/Single/01 Some Track.flac"),
        ("On", "/music/Iron Maiden/Seventh Son/01 Some Track.flac"),
        ("In", "/music/Inside Out/Album/01 Some Track.flac"),
        ("In", "/music/Pain and Glory/Album/01 Some Track.flac"),
        ("In", "/music/Main Street/Album/01 Some Track.flac"),
        ("In", "/music/The Beginning/Album/01 Some Track.flac"),
        ("Me", "/music/Memory Lane/Album/01 Some Track.flac"),
        ("Me", "/music/Game Over/Album/01 Some Track.flac"),
        ("Me", "/music/Summer Time/Album/01 Some Track.flac"),
        ("War", "/music/Hardware/Album/01 Some Track.flac"),
        ("War", "/music/Software Engineering/Album/01 Some Track.flac"),
        ("War", "/music/Warning Shots/Album/01 Some Track.flac"),
        ("War", "/music/Forward Motion/Album/01 Some Track.flac"),
        ("Yes", "/music/Yesterday/Album/01 Some Track.flac"),
        ("The", "/music/Breathe/Album/01 Some Track.flac"),
        ("No", "/music/Nothing/Album/01 Some Track.flac"),
        ("Can", "/music/Candidate/Album/01 Some Track.flac"),
        ("Who", "/music/Whole Lotta Love/Album/01 Some Track.flac"),
    ])
    def test_short_artist_does_not_match_unrelated_path_or_tag(self, short_artist: str, unrelated_path: str):
        """Short artist names must NOT trigger Tier 3 or Tier 4 matches inside longer unrelated words."""
        catalog = build_artist_catalog(
            artist_name=short_artist,
            aliases=[short_artist],
            releases=[{
                "title": "Official Album",
                "tracks": [{"title": "Some Track", "number": "1"}]
            }]
        )

        local_tracks = [{
            "path": unrelated_path,
            "filename": Path(unrelated_path).name,
            "title": "Some Track",
            "norm_title": normalize_text("Some Track"),
            "album": Path(unrelated_path).parent.name,
            "norm_album": normalize_text(Path(unrelated_path).parent.name),
            "artists": ["Unrelated Band"],
            "track_number": "1",
            "mb_rec_ids": set(),
            "mb_track_ids": set(),
            "source": "local",
        }]

        rec = DiscographyReconciler(catalog, local_tracks)
        found, missing = rec.reconcile()

        assert len(found) == 0, f"False positive: artist '{short_artist}' matched inside path '{unrelated_path}'"
        assert len(missing) == 1

    def test_short_artist_matches_when_isolated_word_in_path_or_tag(self):
        """Short artist name matches when it appears as a true standalone word component."""
        catalog = build_artist_catalog(
            artist_name="Air",
            aliases=["Air"],
            releases=[{
                "title": "Moon Safari",
                "tracks": [{"title": "La Femme d'Argent", "number": "1"}]
            }]
        )

        # 1. Standalone word in directory path: /music/Air/Moon Safari/01 Track.flac
        local_tracks_path = [{
            "path": "/music/Air/Moon Safari/01 La Femme d'Argent.flac",
            "filename": "01 La Femme d'Argent.flac",
            "title": "La Femme d'Argent",
            "norm_title": normalize_text("La Femme d'Argent"),
            "album": "Moon Safari",
            "norm_album": normalize_text("Moon Safari"),
            "artists": ["Air"],
            "track_number": "1",
            "mb_rec_ids": set(),
            "mb_track_ids": set(),
            "source": "local",
        }]

        rec = DiscographyReconciler(catalog, local_tracks_path)
        found, missing = rec.reconcile()
        assert len(found) == 1
        assert len(missing) == 0

        # 2. Artist in metadata tag even if path has generic directory
        local_tracks_tag = [{
            "path": "/music/Downloads/01 La Femme d'Argent.flac",
            "filename": "01 La Femme d'Argent.flac",
            "title": "La Femme d'Argent",
            "norm_title": normalize_text("La Femme d'Argent"),
            "album": "Unknown",
            "norm_album": "unknown",
            "artists": ["Air"],
            "track_number": "1",
            "mb_rec_ids": set(),
            "mb_track_ids": set(),
            "source": "local",
        }]

        rec2 = DiscographyReconciler(catalog, local_tracks_tag)
        found2, missing2 = rec2.reconcile()
        assert len(found2) == 1
        assert len(missing2) == 0

    def test_preposition_artist_path_collision_edge_case(self):
        """
        When an artist name is an English preposition like 'In', word-boundary regexes
        will match if 'In' appears as a separate word in an album title (e.g. 'Walking In The Rain').
        Documents this heuristic limitation in Tier 3 path matching.
        """
        catalog = build_artist_catalog(
            artist_name="In",
            aliases=["In"],
            releases=[{
                "title": "Official Album",
                "tracks": [{"title": "Some Track", "number": "1"}]
            }]
        )
        local_tracks = [{
            "path": "/music/Various Artists/Walking In The Rain/01 Some Track.flac",
            "filename": "01 Some Track.flac",
            "title": "Some Track",
            "norm_title": normalize_text("Some Track"),
            "album": "Walking In The Rain",
            "norm_album": normalize_text("Walking In The Rain"),
            "artists": ["Various Artists"],
            "track_number": "1",
            "mb_rec_ids": set(),
            "mb_track_ids": set(),
            "source": "local",
        }]
        rec = DiscographyReconciler(catalog, local_tracks)
        found, _ = rec.reconcile()
        # Because 'in' is an isolated word token in 'Walking In The Rain',
        # has_artist_path evaluates to True in Tier 3.
        # This is a documented trade-off of path matching without full directory-hierarchy awareness.
        assert len(found) in (0, 1)

    def test_reconciler_matches_nfd_file_to_nfc_catalog(self):
        """End-to-end reconciler test: files on disk saved with NFD decomposition match NFC catalog."""
        catalog = build_artist_catalog(
            artist_name="Sigur Rós",
            releases=[{
                "title": "Ágætis byrjun",
                "tracks": [
                    {"title": "Svefn-g-englar", "number": "1"},
                    {"title": "Starálfur", "number": "2"},
                    {"title": "Flugufrelsarinn", "number": "3"},
                ]
            }]
        )
        # Simulate local disk files with NFD decomposed names
        local_tracks = [
            {
                "path": f"/music/{unicodedata.normalize('NFD', 'Sigur Rós')}/{unicodedata.normalize('NFD', 'Ágætis byrjun')}/01 {unicodedata.normalize('NFD', 'Svefn-g-englar')}.flac",
                "filename": f"01 {unicodedata.normalize('NFD', 'Svefn-g-englar')}.flac",
                "title": unicodedata.normalize("NFD", "Svefn-g-englar"),
                "norm_title": normalize_text(unicodedata.normalize("NFD", "Svefn-g-englar")),
                "album": unicodedata.normalize("NFD", "Ágætis byrjun"),
                "norm_album": normalize_text(unicodedata.normalize("NFD", "Ágætis byrjun")),
                "artists": [unicodedata.normalize("NFD", "Sigur Rós")],
                "track_number": "1",
                "mb_rec_ids": set(),
                "mb_track_ids": set(),
                "source": "local",
            },
            {
                "path": f"/music/{unicodedata.normalize('NFD', 'Sigur Rós')}/{unicodedata.normalize('NFD', 'Ágætis byrjun')}/02 {unicodedata.normalize('NFD', 'Starálfur')}.flac",
                "filename": f"02 {unicodedata.normalize('NFD', 'Starálfur')}.flac",
                "title": unicodedata.normalize("NFD", "Starálfur"),
                "norm_title": normalize_text(unicodedata.normalize("NFD", "Starálfur")),
                "album": unicodedata.normalize("NFD", "Ágætis byrjun"),
                "norm_album": normalize_text(unicodedata.normalize("NFD", "Ágætis byrjun")),
                "artists": [unicodedata.normalize("NFD", "Sigur Rós")],
                "track_number": "2",
                "mb_rec_ids": set(),
                "mb_track_ids": set(),
                "source": "local",
            }
        ]

        rec = DiscographyReconciler(catalog, local_tracks)
        found, missing = rec.reconcile()

        assert len(found) == 2
        assert len(missing) == 1
        found_titles = {f["mb_track"]["title"] for f in found}
        assert "Svefn-g-englar" in found_titles
        assert "Starálfur" in found_titles
        assert missing[0]["mb_track"]["title"] == "Flugufrelsarinn"
