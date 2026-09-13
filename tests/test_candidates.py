"""Candidate matching policies can be exercised without constructing a scraper."""

import pytest

from musicscraper.services.candidates import (
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
        "matched_file": "Deep Signal.flac",
        "full_filename": "Atlas\\Archive\\Deep Signal.flac",
        "size": 1000,
    }]
    assert result["unmatched_expected"] == ["Golden Hour"]
    assert result["all_dir_files"] == files
