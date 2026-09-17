"""Disc and vinyl-side prefixes in flat release folders and release audits."""

from unittest.mock import MagicMock

import pytest

from discogseek.core.cache import UnifiedCacheManager
from discogseek.services.library import (
    LibraryReleaseService,
    parse_disc_and_track_number,
)


class TestFlatDiscReleases:
    @pytest.mark.parametrize("raw_track,filename,meta_disc,expected_disc,expected_trk", [
        # Standard tag digits
        ("1", "01 Song.flac", None, 1, 1),
        ("02", "02 Song.flac", 1, 1, 2),
        ("1/12", "01 Song.flac", 1, 1, 1),
        # Disc-track prefix in tag
        ("1-01", "01 Song.flac", 1, 1, 1),
        ("2-01", "01 Song.flac", 1, 2, 1),
        ("2.03", "03 Song.flac", 1, 2, 3),
        ("02-04", "04 Song.flac", 1, 2, 4),
        # Vinyl side notation in tag
        ("A1", "01 Song.flac", 1, 1, 1),
        ("B1", "01 Song.flac", 1, 2, 1),
        ("B2", "02 Song.flac", 1, 2, 2),
        ("C1", "01 Song.flac", 1, 3, 1),
        ("D2", "02 Song.flac", 1, 4, 2),
        ("Side A", "01 Song.flac", 1, 1, 1),
        ("Side B", "01 Song.flac", 1, 2, 1),
        ("Side B 02", "02 Song.flac", 1, 2, 2),
        ("Side 2 - 01", "01 Song.flac", 1, 2, 1),
        ("Disc 2 - 01", "01 Song.flac", 1, 2, 1),
        ("CD 2 - 03", "03 Song.flac", 1, 2, 3),
        # Disc-track prefix in filename (tag has standard digits or untagged)
        ("1", "1-01 Song.flac", 1, 1, 1),
        ("1", "2-01 Song.flac", 1, 2, 1),
        ("2", "2-02 Song.flac", 1, 2, 2),
        (None, "2.01 Song.flac", 1, 2, 1),
        (None, "02-03 Song.flac", 1, 2, 3),
        ("1", "B1 Song.flac", 1, 2, 1),
        ("2", "B02 Song.flac", 1, 2, 2),
        (None, "C1 Song.flac", 1, 3, 1),
        (None, "Side B 01 - Song.flac", 1, 2, 1),
        (None, "Side 2 - 01 Song.flac", 1, 2, 1),
        (None, "Disc 2 - 01 Song.flac", 1, 2, 1),
        (None, "CD 2 - 04 Song.flac", 1, 2, 4),
    ])
    def test_parse_disc_and_track_number_prefixes(
        self,
        raw_track,
        filename,
        meta_disc,
        expected_disc,
        expected_trk
    ):
        """parse_disc_and_track_number accurately extracts disc and track across all prefix variants."""
        d, t, _ = parse_disc_and_track_number(raw_track, filename=filename, meta_disc=meta_disc)
        assert d == expected_disc, f"Expected disc {expected_disc}, got {d} for ({raw_track}, {filename})"
        assert t == expected_trk, f"Expected track {expected_trk}, got {t} for ({raw_track}, {filename})"

    def test_flat_folder_4_vinyl_sides_scan_and_gap_detection(self, tmp_path):
        """
        A double vinyl album with 4 sides in a single flat folder:
        Side A: A1, A2
        Side B: B1 (missing B2)
        Side C: C1, C2, C3
        Side D: D1 (missing D2, D3)
        Must detect 4 distinct discs, correct tracks per disc, and exact missing tracks.
        """
        music_dir = tmp_path / "music"
        album_dir = music_dir / "The Clash" / "London Calling Flat"
        album_dir.mkdir(parents=True)

        # Side A (Disc 1): 1, 2
        (album_dir / "A1 London Calling.flac").write_text("audio")
        (album_dir / "A2 Brand New Cadillac.flac").write_text("audio")

        # Side B (Disc 2): 1 (missing 2)
        (album_dir / "B1 Spanish Bombs.flac").write_text("audio")
        # B2 Missing

        # Side C (Disc 3): 1, 2, 3
        (album_dir / "C1 Clampdown.flac").write_text("audio")
        (album_dir / "C2 The Guns of Brixton.flac").write_text("audio")
        (album_dir / "C3 Wrong 'Em Boyo.flac").write_text("audio")

        # Side D (Disc 4): 1, 3 (missing 2)
        (album_dir / "D1 Lover's Rock.flac").write_text("audio")
        (album_dir / "D3 Revolution Rock.flac").write_text("audio")

        cache = UnifiedCacheManager(db_path=tmp_path / "clash_flat.db")
        service = LibraryReleaseService(cache_manager=cache)

        releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)
        assert len(releases) == 1
        rel = releases[0]

        assert rel["title"] == "London Calling Flat"
        assert rel["artist"] == "The Clash"

        tracks = rel["tracks"]
        found = [t for t in tracks if t["status"] == "found"]
        missing = [t for t in tracks if t["status"] == "missing"]

        assert len(found) == 8  # A1, A2, B1, C1, C2, C3, D1, D3 (8 found)

        # In Side D: 1, 3 found -> missing track 2 detected!
        # Check that Disc 4 missing track 2 was flagged:
        d4_missing = [t for t in missing if t["disc_number"] == 4 and t["track_num_int"] == 2]
        assert len(d4_missing) == 1, "Disc 4 Track 2 gap was not detected!"

        # Ensure discs 1, 2, 3, 4 are all represented
        discs_found = {t["disc_number"] for t in found}
        assert discs_found == {1, 2, 3, 4}

    def test_flat_folder_audit_release_against_musicbrainz_multidisc(self):
        """
        Auditing a flat multi-disc release with '1-01', '1-02', '2-01', '2-02' against MusicBrainz.
        Disc 1 has track 1 and 2.
        Disc 2 has track 1 and 2.
        MusicBrainz has Disc 1 (tracks 1, 2) and Disc 2 (tracks 1, 2, 3).
        Disc 2 Track 3 must be marked missing; all other tracks found with zero collisions.
        """
        mock_mb = MagicMock()
        mock_mb.get_release_by_id.return_value = {
            "id": "mb-flat-audit-stress",
            "title": "Flat Multi Album",
            "artist-credit": [{"name": "Rock Band"}],
            "release-group": {"primary-type": "Album", "secondary-type-list": []},
            "medium-list": [
                {
                    "position": 1,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "D1 Song 1", "recording": {"id": "d1-1"}},
                        {"number": "2", "position": 2, "title": "D1 Song 2", "recording": {"id": "d1-2"}},
                    ]
                },
                {
                    "position": 2,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "D2 Song 1", "recording": {"id": "d2-1"}},
                        {"number": "2", "position": 2, "title": "D2 Song 2", "recording": {"id": "d2-2"}},
                        {"number": "3", "position": 3, "title": "D2 Song 3", "recording": {"id": "d2-3"}},
                    ]
                }
            ]
        }

        service = LibraryReleaseService(mb_client=mock_mb)

        release_data = {
            "artist": "Rock Band",
            "title": "Flat Multi Album",
            "mb_release_id": "mb-flat-audit-stress",
            "tracks": [
                # Notice disc_number is 1 or None originally (simulating flat folder entry before normalization)
                {"disc_number": 1, "track_number": "1", "track_num_int": 1, "title": "D1 Song 1", "filename": "1-01 D1 Song 1.flac", "path": "/music/1-01 D1 Song 1.flac"},
                {"disc_number": 1, "track_number": "2", "track_num_int": 2, "title": "D1 Song 2", "filename": "1-02 D1 Song 2.flac", "path": "/music/1-02 D1 Song 2.flac"},
                {"disc_number": 1, "track_number": "1", "track_num_int": 1, "title": "D2 Song 1", "filename": "2-01 D2 Song 1.flac", "path": "/music/2-01 D2 Song 1.flac"},
                {"disc_number": 1, "track_number": "2", "track_num_int": 2, "title": "D2 Song 2", "filename": "2-02 D2 Song 2.flac", "path": "/music/2-02 D2 Song 2.flac"},
            ]
        }

        audited = service.audit_release(release_data)

        assert audited["status"] == "has_missing"
        assert audited["found_count"] == 4
        assert audited["missing_count"] == 1

        # Check Disc 2 Track 3 is the only missing track
        missing = [t for t in audited["tracks"] if t["status"] == "missing"]
        assert len(missing) == 1
        assert missing[0]["disc_number"] == 2
        assert missing[0]["track_number"] == "3"
        assert missing[0]["title"] == "D2 Song 3"

        # Check all 4 local files are matched as found with correct titles
        found = [t for t in audited["tracks"] if t["status"] == "found"]
        assert len(found) == 4
        found_d1 = [t for t in found if t["disc_number"] == 1]
        found_d2 = [t for t in found if t["disc_number"] == 2]
        assert len(found_d1) == 2
        assert len(found_d2) == 2

        # Check zero unmatched bonus tracks appended
        assert len(audited["tracks"]) == 5

    def test_flat_folder_purely_numeric_multidisc_audit(self):
        """
        Purely numeric files in flat structure:
        '1-01.flac', '1-02.flac', '2-01.flac', '2-02.flac'
        Must resolve to official song titles without collision or false negatives.
        """
        mock_mb = MagicMock()
        mock_mb.get_release_by_id.return_value = {
            "id": "mb-flat-num-stress",
            "title": "Numeric Multi Album",
            "artist-credit": [{"name": "Ambient Band"}],
            "release-group": {"primary-type": "Album", "secondary-type-list": []},
            "medium-list": [
                {
                    "position": 1,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "First Movement", "recording": {"id": "rec-1"}},
                        {"number": "2", "position": 2, "title": "Second Movement", "recording": {"id": "rec-2"}},
                    ]
                },
                {
                    "position": 2,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Third Movement", "recording": {"id": "rec-3"}},
                        {"number": "2", "position": 2, "title": "Fourth Movement", "recording": {"id": "rec-4"}},
                    ]
                }
            ]
        }

        service = LibraryReleaseService(mb_client=mock_mb)

        release_data = {
            "artist": "Ambient Band",
            "title": "Numeric Multi Album",
            "mb_release_id": "mb-flat-num-stress",
            "tracks": [
                {"disc_number": 1, "track_number": "1", "track_num_int": 1, "title": "", "filename": "1-01.flac", "path": "/music/1-01.flac"},
                {"disc_number": 1, "track_number": "2", "track_num_int": 2, "title": "", "filename": "1-02.flac", "path": "/music/1-02.flac"},
                {"disc_number": 1, "track_number": "1", "track_num_int": 1, "title": "", "filename": "2-01.flac", "path": "/music/2-01.flac"},
                {"disc_number": 1, "track_number": "2", "track_num_int": 2, "title": "", "filename": "2-02.flac", "path": "/music/2-02.flac"},
            ]
        }

        audited = service.audit_release(release_data)

        assert audited["status"] == "complete"
        assert audited["found_count"] == 4
        assert audited["missing_count"] == 0
        assert len(audited["tracks"]) == 4

        d1_t1 = next(t for t in audited["tracks"] if t["disc_number"] == 1 and t["track_number"] == "1")
        d1_t2 = next(t for t in audited["tracks"] if t["disc_number"] == 1 and t["track_number"] == "2")
        d2_t1 = next(t for t in audited["tracks"] if t["disc_number"] == 2 and t["track_number"] == "1")
        d2_t2 = next(t for t in audited["tracks"] if t["disc_number"] == 2 and t["track_number"] == "2")

        assert d1_t1["title"] == "First Movement"
        assert d1_t2["title"] == "Second Movement"
        assert d2_t1["title"] == "Third Movement"
        assert d2_t2["title"] == "Fourth Movement"
