"""Reconciliation of numbered tracks and rejection of conflicting numbers."""

import pytest

from discogseek.core.text import normalize_text
from discogseek.services.reconciler import (
    DiscographyReconciler,
    have_conflicting_numbers,
)
from tests.helpers.catalog import build_artist_catalog


class TestTrackNumberMatching:
    @pytest.mark.parametrize("title_a,title_b", [
        ("Movement 1", "Movement 2"),
        ("Movement I", "Movement II"),
        ("Untitled 1", "Untitled 2"),
        ("Untitled 01", "Untitled 02"),
        ("Part 1", "Part 2"),
        ("Part I", "Part IV"),
        ("Act 1", "Act 9"),
        ("Sonata No. 1", "Sonata No. 2"),
        ("Track 01", "Track 02"),
        ("Suite 1", "Suite 2"),
        ("Symphony No. 5", "Symphony No. 9"),
    ])
    def test_have_conflicting_numbers_detector(self, title_a: str, title_b: str):
        """Detector flags conflicting numbers between distinct numbered tracks."""
        assert have_conflicting_numbers(title_a, title_b) is True
        assert have_conflicting_numbers(title_b, title_a) is True

    @pytest.mark.parametrize("title_a,title_b", [
        ("Movement 1", "Movement 1"),
        ("Movement I", "Movement 1"),
        ("Part IV", "Part 4"),
        ("Act IX", "Act 9"),
        ("Track 01", "Track 1"),
        ("Suite No. II", "Suite No. 2"),
    ])
    def test_have_conflicting_numbers_false_for_equivalent(self, title_a: str, title_b: str):
        """Detector does NOT flag conflicts when numbers are numerically equivalent."""
        assert have_conflicting_numbers(title_a, title_b) is False

    def test_reconciler_never_matches_differing_numbered_tracks(self):
        """Catalog 'Movement 1' and 'Movement 2'; only 'Movement 2' on disk. 'Movement 1' MUST be missing."""
        catalog = build_artist_catalog(
            artist_name="Composer",
            releases=[{
                "title": "Symphony",
                "tracks": [
                    {"title": "Movement 1", "number": "1"},
                    {"title": "Movement 2", "number": "2"},
                ]
            }]
        )

        local_tracks = [{
            "path": "/music/Composer/Symphony/02 Movement 2.flac",
            "filename": "02 Movement 2.flac",
            "title": "Movement 2",
            "norm_title": normalize_text("Movement 2"),
            "album": "Symphony",
            "norm_album": normalize_text("Symphony"),
            "artists": ["Composer"],
            "track_number": "2",
            "mb_rec_ids": set(),
            "mb_track_ids": set(),
            "source": "local",
        }]

        rec = DiscographyReconciler(catalog, local_tracks)
        found, missing = rec.reconcile()

        assert len(found) == 1
        assert found[0]["mb_track"]["title"] == "Movement 2"
        assert len(missing) == 1
        assert missing[0]["mb_track"]["title"] == "Movement 1"

    def test_reconciler_untitled_numeric_tracks(self):
        """Catalog 'Untitled 1' and 'Untitled 2'; only 'Untitled 2' on disk."""
        catalog = build_artist_catalog(
            artist_name="Ambient Artist",
            releases=[{
                "title": "Untitled Album",
                "tracks": [
                    {"title": "Untitled 1", "number": "1"},
                    {"title": "Untitled 2", "number": "2"},
                ]
            }]
        )

        local_tracks = [{
            "path": "/music/Ambient Artist/Untitled Album/02 Untitled 2.flac",
            "filename": "02 Untitled 2.flac",
            "title": "Untitled 2",
            "norm_title": normalize_text("Untitled 2"),
            "album": "Untitled Album",
            "norm_album": normalize_text("Untitled Album"),
            "artists": ["Ambient Artist"],
            "track_number": "2",
            "mb_rec_ids": set(),
            "mb_track_ids": set(),
            "source": "local",
        }]

        rec = DiscographyReconciler(catalog, local_tracks)
        found, missing = rec.reconcile()

        assert len(found) == 1
        assert found[0]["mb_track"]["title"] == "Untitled 2"
        assert len(missing) == 1
        assert missing[0]["mb_track"]["title"] == "Untitled 1"

    def test_numeric_filename_in_unconfirmed_album_fails(self):
        """Loose '01.flac' in unconfirmed folder without artist or album must NOT match catalog track."""
        catalog = build_artist_catalog(
            artist_name="Band",
            releases=[{
                "title": "Album One",
                "tracks": [{"title": "Epic Track", "number": "1"}]
            }]
        )

        local_tracks = [{
            "path": "/music/Downloads/01.flac",
            "filename": "01.flac",
            "title": "",
            "norm_title": "",
            "album": "Downloads",
            "norm_album": "downloads",
            "artists": [],
            "track_number": "1",
            "mb_rec_ids": set(),
            "mb_track_ids": set(),
            "source": "local",
        }]

        rec = DiscographyReconciler(catalog, local_tracks)
        found, missing = rec.reconcile()

        assert len(found) == 0, "Purely numeric track in unconfirmed album must not match"
        assert len(missing) == 1

    @pytest.mark.parametrize("title_a,title_b", [
        ("Part I", "Part II"),
        ("Part III", "Part IV"),
        ("Part VII", "Part VIII"),
        ("Part IX", "Part X"),
        ("Part XIV", "Part XV"),
        ("Movement 1", "Movement 2"),
        ("Movement I", "Movement 2"),
        ("Act 1", "Act 2"),
        ("Suite No. 1", "Suite No. 2"),
        ("Sonata No. 3", "Sonata No. 4"),
        ("Opus 1", "Opus 2"),
    ])
    def test_have_conflicting_numbers_flags_differences(self, title_a: str, title_b: str):
        """Differing Roman/Arabic numbered tracks are recognized as conflicting."""
        assert have_conflicting_numbers(title_a, title_b) is True
        assert have_conflicting_numbers(title_b, title_a) is True

    @pytest.mark.parametrize("title_a,title_b", [
        ("Part I", "Part 1"),
        ("Part IV", "Part 4"),
        ("Part IX", "Part 9"),
        ("Part XIV", "Part 14"),
        ("Part XX", "Part 20"),
        ("Movement II", "Movement 2"),
        ("Act III", "Act 3"),
        ("Suite No. V", "Suite No. 5"),
    ])
    def test_have_conflicting_numbers_recognizes_equivalents(self, title_a: str, title_b: str):
        """Equivalent Roman/Arabic numbered tracks are NOT flagged as conflicting."""
        assert have_conflicting_numbers(title_a, title_b) is False
        assert have_conflicting_numbers(title_b, title_a) is False
