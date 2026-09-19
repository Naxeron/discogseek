"""Peer candidate indexing, directory expansion, and track selection."""

from unittest.mock import MagicMock

import pytest

from discogseek.services.candidates import (
    CandidateDir,
    PeerCandidateIndex,
    evaluate_directory,
    find_best_track_candidate,
    pre_parse_expected_tracks,
)


@pytest.mark.parametrize(
    ("preferred_format", "expected_peer"),
    [("flac", "lossless-peer"), ("mp3", "available-peer")],
)
def test_track_selection_balances_format_preference_and_queue(preferred_format, expected_peer):
    index = PeerCandidateIndex({
        ("lossless-peer", "Atlas"): {
            "matched_search_files": [{"filename": "Atlas\\Deep Signal.flac"}],
            "queue": 80,
        },
        ("available-peer", "Atlas"): {
            "matched_search_files": [{"filename": "Atlas\\Deep Signal.mp3", "bitRate": 320}],
            "queue": 0,
        },
    })

    selected = find_best_track_candidate(index, "Deep Signal", {"Atlas"}, preferred_format)

    assert selected.user == expected_peer

def test_track_selection_rejects_live_version_for_studio_track():
    index = PeerCandidateIndex({
        ("peer", "Atlas"): {
            "matched_search_files": [{"filename": "Atlas\\Deep Signal (Live).flac"}],
        },
    })

    assert find_best_track_candidate(index, "Deep Signal", {"Atlas"}) is None
    assert find_best_track_candidate(index, "Deep Signal (Live)", {"Atlas"}) is not None

def test_short_title_requires_artist_or_release_context():
    index = PeerCandidateIndex({
        ("peer", "Archive"): {
            "matched_search_files": [{"filename": "Archive\\Go.flac"}],
        },
    })

    assert find_best_track_candidate(index, "Go", {"Atlas"}) is None
    assert find_best_track_candidate(index, "Go", {"Atlas"}, rel_title="Archive") is not None

def test_directory_evaluation_keeps_partial_coverage_and_supporting_files():
    files = [
        {"filename": "Atlas\\Archive\\Deep Signal.flac", "size": 1000},
        {"filename": "Atlas\\Archive\\cover.jpg", "size": 100},
    ]
    candidate = CandidateDir("peer", "Atlas\\Archive", {
        "matched_search_files": files,
        "queue": 10,
        "speed": 1000,
        "has_slot": True,
    })
    expected = [{"title": "Deep Signal"}, {"title": "Golden Hour"}]

    result = evaluate_directory(
        candidate,
        pre_parse_expected_tracks(expected),
        expected,
        "Archive",
        {"Atlas"},
    )

    assert result["match_ratio"] == 0.5
    assert result["total_score"] == 190
    assert result["matched_tracks"] == [{
        "expected": "Deep Signal",
        "expected_index": 0,
        "matched_file": "Deep Signal.flac",
        "full_filename": "Atlas\\Archive\\Deep Signal.flac",
        "size": 1000,
    }]
    assert result["unmatched_expected"] == ["Golden Hour"]
    assert result["all_dir_files"] == files

def test_candidate_dir_deduplication():
    dir_info = {
        "matched_search_files": [
            {"filename": "Music\\Artist\\Album\\01 track.flac", "size": 1000},
            {"filename": "Music\\Artist\\Album\\01 track.flac", "size": 1000},
            {"filename": "Music\\Artist\\Album\\02 track.flac", "size": 2000},
            {"filename": "music/artist/album/02 track.flac", "size": 2000},
        ]
    }
    cd = CandidateDir(user="test_peer", dir_name="Album", dir_info=dir_info)
    assert len(cd.all_dir_files) == 2
    assert len(cd.audio_files) == 2

def test_candidate_dir_remote_expansion():
    from discogseek.clients.musicbrainz import ArtistCatalog
    from discogseek.services.soulseek import PeerCandidateIndex, SlskdArtistScraper

    raw_data = {
        "artist": {"id": "art-1", "name": "Artist"},
        "releases_artist": [
            {
                "id": "rel-1",
                "title": "Awesome Album",
                "medium-list": [
                    {
                        "track-list": [
                            {"id": "t1", "title": "First Song"},
                            {"id": "t2", "title": "Second Song"},
                        ]
                    }
                ]
            }
        ],
        "releases_track_artist": [],
        "recordings": []
    }
    cat = ArtistCatalog(raw_data)
    scraper = SlskdArtistScraper(artist_query="Artist", dry_run=True)
    scraper.catalog = cat
    scraper.all_artist_aliases = {"artist"}

    # Simulate a peer directory where search results only returned track 1
    mock_client = MagicMock()
    mock_client.browse_directories_batch.return_value = {
        ("peer1", "Music\\Artist - Awesome Album"): [
            {"filename": "Music\\Artist - Awesome Album\\01 First Song.flac", "size": 1000},
            {"filename": "Music\\Artist - Awesome Album\\02 Second Song.flac", "size": 2000},
        ]
    }
    scraper.client = mock_client

    scraper.peer_directories = {
        ("peer1", "Music\\Artist - Awesome Album"): {
            "user": "peer1",
            "directory": "Music\\Artist - Awesome Album",
            "matched_search_files": [
                {"filename": "Music\\Artist - Awesome Album\\01 First Song.flac", "size": 1000}
            ],
            "full_directory_files": None,
            "speed": 1000,
            "queue": 0,
            "has_slot": True,
        }
    }
    scraper.candidate_index = PeerCandidateIndex(scraper.peer_directories)

    scraper._reconcile_primary_releases()

    # browse_directories_batch should have been called to expand the album from 1 track to 2 tracks
    mock_client.browse_directories_batch.assert_called_once()
    assert len(scraper.verified_releases) == 1
    assert scraper.verified_releases[0]["matched_count"] == 2
    assert scraper.verified_releases[0]["total_count"] == 2

def test_candidate_indexing_scalability_and_speed():
    """
    SCALABILITY BENCHMARK:
    Verifies that PeerCandidateIndex scales to 5,000 candidate audio files across
    250 directories, indexing in <1s and pruning candidate searches in <50ms.
    """
    import time

    from discogseek.services.soulseek import PeerCandidateIndex

    peer_dirs = {}
    for d in range(250):
        files = [
            {
                "filename": f"Music\\Artist\\Album_{d}\\{i:02d} unique_track_{d}_{i}.flac",
                "size": 25000000 + i * 1000,
                "bitrate": 950,
                "bitDepth": 16,
                "sampleRate": 44100,
            }
            for i in range(20)
        ]
        peer_dirs[(f"peer_{d}", f"Album_{d}")] = {
            "user": f"peer_{d}",
            "directory": f"Album_{d}",
            "matched_search_files": files,
            "full_directory_files": None,
            "speed": 1000000,
            "queue": 0,
            "has_slot": True,
        }

    t0_idx = time.perf_counter()
    index = PeerCandidateIndex(peer_dirs)
    idx_duration = time.perf_counter() - t0_idx

    assert len(index.all_audio_files) == 5000
    assert len(index.all_dirs) == 250
    assert idx_duration < 1.0, f"Indexing 5000 files took {idx_duration:.4f}s; expected < 1.0s"

    # Query candidate directories for a specific release
    t0_query = time.perf_counter()
    cand_dirs = index.get_candidate_dirs_for_release(
        rel_title="Album_42",
        parsed_expected=[{"sig_words": {"unique", "track", "42", "5"}}],
        expected_count=20
    )
    query_duration = time.perf_counter() - t0_query

    assert query_duration < 0.05, f"Directory query took {query_duration:.4f}s; expected < 0.05s"
    assert any(cd.dir_name == "Album_42" for cd in cand_dirs)
    assert len(cand_dirs) <= 10, f"Inverted index should prune 250 dirs down to candidate match; got {len(cand_dirs)}"

def test_candidate_matching_format_quality_scoring():
    """Verifies that candidate matching evaluates and scores audio formats (FLAC > MP3 320 > MP3 128)."""
    from discogseek.services.soulseek import CandidateFile

    dir_info = {"speed": 1000, "queue": 0, "has_slot": True}

    flac_file = CandidateFile(
        user="peer1",
        dir_name="Album",
        raw_file={"filename": "Music\\Album\\01 track.flac", "size": 30000000, "bitDepth": 16, "sampleRate": 44100},
        dir_info=dir_info
    )
    mp3_320 = CandidateFile(
        user="peer1",
        dir_name="Album",
        raw_file={"filename": "Music\\Album\\01 track.mp3", "size": 8000000, "bitRate": 320},
        dir_info=dir_info
    )
    mp3_128 = CandidateFile(
        user="peer1",
        dir_name="Album",
        raw_file={"filename": "Music\\Album\\01 track.mp3", "size": 3000000, "bitRate": 128},
        dir_info=dir_info
    )

    assert flac_file.is_audio is True
    assert flac_file.fmt_score > mp3_320.fmt_score > mp3_128.fmt_score
    assert "lossless" in flac_file.fmt_label.lower() or "flac" in flac_file.fmt_label.lower()
