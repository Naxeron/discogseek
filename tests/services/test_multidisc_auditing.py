"""Multi-disc release audits keep catalog matches and missing tracks on the correct disc."""

from unittest.mock import MagicMock

from discogseek.core.cache import UnifiedCacheManager
from discogseek.services.library import LibraryReleaseService
from discogseek.services.reconciler import (
    have_conflicting_numbers,
    have_conflicting_track_numbers,
    is_track_number_match,
)


class TestMultiDiscAuditing:
    """Reconcile multi-disc releases without cross-disc track matches."""

    def test_audit_release_multidisc_reconciliation_zero_false_negatives(self):
        """
        Auditing a multi-disc release against MusicBrainz data:
        Disc 1 is missing track 2; Disc 2 has track 2.
        Ensure MusicBrainz reconciler marks Disc 1 Track 2 as missing and Disc 2 Track 2 as found.
        """
        mock_mb = MagicMock()
        mock_mb.get_release_by_id.return_value = {
            "id": "mb-multi-123",
            "title": "Double Album",
            "artist-credit": [{"name": "The Band"}],
            "release-group": {"primary-type": "Album", "secondary-type-list": []},
            "medium-list": [
                {
                    "position": 1,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Track 1.1", "recording": {"id": "rec-1-1"}},
                        {"number": "2", "position": 2, "title": "Track 1.2", "recording": {"id": "rec-1-2"}},
                        {"number": "3", "position": 3, "title": "Track 1.3", "recording": {"id": "rec-1-3"}},
                    ]
                },
                {
                    "position": 2,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Track 2.1", "recording": {"id": "rec-2-1"}},
                        {"number": "2", "position": 2, "title": "Track 2.2", "recording": {"id": "rec-2-2"}},
                        {"number": "3", "position": 3, "title": "Track 2.3", "recording": {"id": "rec-2-3"}},
                    ]
                }
            ]
        }

        service = LibraryReleaseService(mb_client=mock_mb)

        release_data = {
            "artist": "The Band",
            "title": "Double Album",
            "mb_release_id": "mb-multi-123",
            "tracks": [
                {"disc_number": 1, "track_number": "1", "track_num_int": 1, "title": "Track 1.1", "filename": "01.flac", "path": "/music/1/01.flac"},
                {"disc_number": 1, "track_number": "3", "track_num_int": 3, "title": "Track 1.3", "filename": "03.flac", "path": "/music/1/03.flac"},
                {"disc_number": 2, "track_number": "1", "track_num_int": 1, "title": "Track 2.1", "filename": "01.flac", "path": "/music/2/01.flac"},
                {"disc_number": 2, "track_number": "2", "track_num_int": 2, "title": "Track 2.2", "filename": "02.flac", "path": "/music/2/02.flac"},
                {"disc_number": 2, "track_number": "3", "track_num_int": 3, "title": "Track 2.3", "filename": "03.flac", "path": "/music/2/03.flac"},
            ]
        }

        audited = service.audit_release(release_data)
        assert audited["status"] == "has_missing"
        assert audited["found_count"] == 5
        assert audited["missing_count"] == 1

        missing_tracks = [t for t in audited["tracks"] if t["status"] == "missing"]
        assert len(missing_tracks) == 1
        m = missing_tracks[0]
        assert m["disc_number"] == 1
        assert m["track_number"] == "2"
        assert m["title"] == "Track 1.2"

        # Disc 2 track 2 must be found
        found_d2_t2 = [t for t in audited["tracks"] if t["status"] == "found" and t["disc_number"] == 2 and t["track_number"] == "2"]
        assert len(found_d2_t2) == 1
        assert found_d2_t2[0]["title"] == "Track 2.2"

    def test_flat_folder_disc_prefixes_match_catalog_discs(self, tmp_path):
        """Files with disc-track prefixes match the corresponding catalog disc.

        Disc 2 files must match medium 2 instead of becoming unmatched bonus
        tracks while their catalog counterparts are marked missing."""
        music_dir = tmp_path / "music"
        album_dir = music_dir / "Artist" / "Flat Audit Double"
        album_dir.mkdir(parents=True)

        (album_dir / "1-01 Song 1.flac").write_text("1")
        (album_dir / "1-02 Song 2.flac").write_text("2")
        (album_dir / "2-01 Song 3.flac").write_text("3")
        (album_dir / "2-02 Song 4.flac").write_text("4")

        mock_mb = MagicMock()
        mock_mb.get_release_by_id.return_value = {
            "id": "mb-flat-probe-3",
            "title": "Flat Audit Double",
            "artist-credit": [{"name": "Artist"}],
            "release-group": {"primary-type": "Album", "secondary-type-list": []},
            "medium-list": [
                {
                    "position": 1,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Song 1", "recording": {"id": "r1"}},
                        {"number": "2", "position": 2, "title": "Song 2", "recording": {"id": "r2"}},
                    ]
                },
                {
                    "position": 2,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Song 3", "recording": {"id": "r3"}},
                        {"number": "2", "position": 2, "title": "Song 4", "recording": {"id": "r4"}},
                    ]
                }
            ]
        }

        cache = UnifiedCacheManager(db_path=tmp_path / "flat_probe_3.db")
        service = LibraryReleaseService(mb_client=mock_mb, cache_manager=cache)

        releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)
        assert len(releases) == 1
        rel = releases[0]

        audited = service.audit_release(rel)
        assert audited["missing_count"] == 0, (
            f"CRITICAL BUG: Reconciler reported {audited['missing_count']} missing tracks! "
            f"Disc 2 tracks were marked missing despite existing on disk as '2-01' and '2-02'."
        )

    def test_audit_release_multidisc_completely_missing_disc(self, tmp_path):
        """
        Verify audit_release when an official release has 4 discs,
        and local files exist for Discs 1, 2, and 4, but Disc 3 is completely missing.
        All tracks of Disc 3 must be marked missing, with zero cross-disc pollution.
        """
        music_dir = tmp_path / "music"
        album_dir = music_dir / "Artist" / "Four Disc Epic"
        d1 = album_dir / "Disc 1"
        d2 = album_dir / "Disc 2"
        d4 = album_dir / "Disc 4"
        for d in (d1, d2, d4):
            d.mkdir(parents=True)

        (d1 / "01 Song 1.flac").write_text("1")
        (d1 / "02 Song 2.flac").write_text("2")

        (d2 / "01 Song 3.flac").write_text("3")
        (d2 / "03 Song 4.flac").write_text("4")

        (d4 / "02 Song 7.flac").write_text("7")

        mock_mb = MagicMock()
        mock_mb.get_release_by_id.return_value = {
            "id": "mb-4disc-complete-missing",
            "title": "Four Disc Epic",
            "artist-credit": [{"name": "Artist"}],
            "release-group": {"primary-type": "Album", "secondary-type-list": []},
            "medium-list": [
                {
                    "position": 1,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Song 1", "recording": {"id": "r1"}},
                        {"number": "2", "position": 2, "title": "Song 2", "recording": {"id": "r2"}},
                    ]
                },
                {
                    "position": 2,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Song 3", "recording": {"id": "r3"}},
                        {"number": "2", "position": 2, "title": "Song 3-Missing", "recording": {"id": "r3m"}},
                        {"number": "3", "position": 3, "title": "Song 4", "recording": {"id": "r4"}},
                    ]
                },
                {
                    "position": 3,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Song 5", "recording": {"id": "r5"}},
                        {"number": "2", "position": 2, "title": "Song 6", "recording": {"id": "r6"}},
                    ]
                },
                {
                    "position": 4,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Song 7-Missing", "recording": {"id": "r7m"}},
                        {"number": "2", "position": 2, "title": "Song 7", "recording": {"id": "r7"}},
                    ]
                }
            ]
        }

        cache = UnifiedCacheManager(db_path=tmp_path / "audit_4disc.db")
        service = LibraryReleaseService(mb_client=mock_mb, cache_manager=cache)

        releases = service.scan_library_releases(library_dir=music_dir, force_rescan=True)
        rel = releases[0]
        rel["mb_release_id"] = "mb-4disc-complete-missing"

        audited = service.audit_release(rel)
        assert audited["status"] == "has_missing"
        assert audited["found_count"] == 5
        assert audited["missing_count"] == 4  # Disc 2 trk 2, Disc 3 trk 1 & 2, Disc 4 trk 1

        # Verify Disc 3 has 0 found, 2 missing
        d3_tracks = [t for t in audited["tracks"] if t["disc_number"] == 3]
        assert len(d3_tracks) == 2
        assert all(t["status"] == "missing" for t in d3_tracks)
        assert [t["title"] for t in d3_tracks] == ["Song 5", "Song 6"]

        # Verify Disc 4 has 1 found (Song 7) and 1 missing (Song 7-Missing)
        d4_tracks = [t for t in audited["tracks"] if t["disc_number"] == 4]
        assert len(d4_tracks) == 2
        found_d4 = next(t for t in d4_tracks if t["status"] == "found")
        missing_d4 = next(t for t in d4_tracks if t["status"] == "missing")
        assert found_d4["title"] == "Song 7"
        assert found_d4["track_number"] == "2"
        assert missing_d4["title"] == "Song 7-Missing"
        assert missing_d4["track_number"] == "1"

    def test_reconciler_prevents_cross_disc_track_collision(self):
        """
        Local release has Disc 1 Track 1 ("Intro") and Disc 2 Track 1 ("Overture").
        MusicBrainz catalog has Disc 1 Track 1 ("Intro") and Disc 2 Track 1 ("Overture").
        Disc numbers must prevent Disc 1 Track 1 from matching Disc 2 Track 1.
        """
        mock_mb = MagicMock()
        mock_mb.get_release_by_id.return_value = {
            "id": "mb-collision-test",
            "title": "Double Concept Album",
            "artist-credit": [{"name": "Prog Rockers"}],
            "release-group": {"primary-type": "Album", "secondary-type-list": []},
            "medium-list": [
                {
                    "position": 1,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Intro", "recording": {"id": "rec-1-1"}},
                        {"number": "2", "position": 2, "title": "Main Theme", "recording": {"id": "rec-1-2"}},
                    ]
                },
                {
                    "position": 2,
                    "track-list": [
                        {"number": "1", "position": 1, "title": "Overture", "recording": {"id": "rec-2-1"}},
                        {"number": "2", "position": 2, "title": "Finale", "recording": {"id": "rec-2-2"}},
                    ]
                }
            ]
        }

        service = LibraryReleaseService(mb_client=mock_mb)

        # Local files: Disc 1 Track 1, Disc 2 Track 1
        release_data = {
            "id": "local-rel",
            "title": "Double Concept Album",
            "artist": "Prog Rockers",
            "mb_release_id": "mb-collision-test",
            "tracks": [
                {"disc_number": 1, "track_number": "1", "track_num_int": 1, "title": "Intro", "filename": "1-01 Intro.flac"},
                {"disc_number": 2, "track_number": "1", "track_num_int": 1, "title": "Overture", "filename": "2-01 Overture.flac"},
            ]
        }

        audited = service.audit_release(release_data=release_data)
        tracks = audited["tracks"]

        # Exactly 2 found and 2 missing
        found = [t for t in tracks if t["status"] == "found"]
        missing = [t for t in tracks if t["status"] == "missing"]

        assert len(found) == 2
        assert len(missing) == 2

        # Found must align with correct discs
        d1_f = next(t for t in found if t["disc_number"] == 1)
        d2_f = next(t for t in found if t["disc_number"] == 2)

        assert d1_f["title"] == "Intro"
        assert d2_f["title"] == "Overture"

    def test_reconciler_track_number_conflict_guardrail(self):
        """
        Different track numbers (Track 1 vs Track 2) must never match even if
        titles are somewhat similar.
        """
        assert not is_track_number_match("1", "2")
        assert not is_track_number_match("01", "02")
        assert have_conflicting_numbers("Track 1", "Track 2")
        assert have_conflicting_track_numbers("1", "2")
