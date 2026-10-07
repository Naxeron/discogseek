"""Artist download pre-scans, search queries, and partial-album candidates."""



def test_query_generation_includes_user_query_and_canonical():
    from discogseek.clients.musicbrainz import ArtistCatalog
    from discogseek.services.soulseek import SlskdArtistScraper

    raw_data = {
        "artist": {"id": "art-jp", "name": "すてらべえ"},
        "releases_artist": [
            {
                "id": "rel-1",
                "title": "Breakcore Forever",
                "medium-list": [{"track-list": [{"id": "t1", "title": "Track 1"}]}]
            }
        ],
        "releases_track_artist": [],
        "recordings": []
    }
    cat = ArtistCatalog(raw_data)
    scraper = SlskdArtistScraper(artist_query="Stellabee", dry_run=True)
    scraper.catalog = cat

    queries = scraper._generate_all_search_queries()
    # Must include user query, canonical name, and release queries
    assert "Stellabee" in queries
    assert "すてらべえ" in queries
    assert "Stellabee Breakcore Forever" in queries
    assert "すてらべえ Breakcore Forever" in queries
    # Must NOT produce mangled unidecode "suterabee Breakcore Forever"
    assert "suterabee Breakcore Forever" not in queries

def test_partial_album_candidate_matching_selective_queue(monkeypatch):
    """
    Verifies that when a release is partially satisfied locally (e.g. tracks 1 and 2 present),
    the downloader enqueues ONLY the genuinely missing track (track 3) from the remote candidate directory.
    """
    from discogseek.clients.musicbrainz import ArtistCatalog
    from discogseek.core.text import normalize_text
    from discogseek.services.soulseek import (
        CandidateDir,
        SlskdArtistScraper,
        pre_parse_expected_tracks,
    )

    raw_data = {
        "artist": {"id": "art-partial", "name": "Target Artist"},
        "releases_artist": [
            {
                "id": "rel-partial-1",
                "title": "Three Track EP",
                "medium-list": [
                    {
                        "track-list": [
                            {"id": "t1", "title": "Track One"},
                            {"id": "t2", "title": "Track Two"},
                            {"id": "t3", "title": "Track Three"},
                        ]
                    }
                ]
            }
        ],
        "releases_track_artist": [],
        "recordings": []
    }

    catalog = ArtistCatalog(raw_data)
    scraper = SlskdArtistScraper(artist_query="Target Artist", dry_run=True)
    scraper.catalog = catalog
    scraper.all_artist_aliases = {"target artist"}

    # Tracks 1 and 2 already present locally
    scraper.local_found_map = {
        "track one": {"path": "/music/Target Artist/Three Track EP/01 Track One.flac"},
        "track two": {"path": "/music/Target Artist/Three Track EP/02 Track Two.flac"},
    }

    cand_dir_files = [
        {"filename": "Music\\Target Artist - Three Track EP\\01 Track One.flac", "size": 1000},
        {"filename": "Music\\Target Artist - Three Track EP\\02 Track Two.flac", "size": 2000},
        {"filename": "Music\\Target Artist - Three Track EP\\03 Track Three.flac", "size": 3000},
    ]

    cd = CandidateDir(
        user="peer_full",
        dir_name="Target Artist - Three Track EP",
        dir_info={
            "user": "peer_full",
            "directory": "Target Artist - Three Track EP",
            "matched_search_files": cand_dir_files,
            "full_directory_files": cand_dir_files,
            "speed": 5000,
            "queue": 0,
            "has_slot": True,
        }
    )

    expected_tracks = catalog.tracks
    parsed_expected = pre_parse_expected_tracks(expected_tracks)

    # Reconcile release with candidate directory
    res = scraper._evaluate_indexed_directory(cd, parsed_expected, expected_tracks, "Three Track EP")
    assert res is not None
    assert len(res["matched_tracks"]) == 3

    # Missing track filtering: only track three is missing locally
    missing_to_queue = [
        m for m in res["matched_tracks"]
        if normalize_text(m["expected"]) not in scraper.local_found_map
    ]
    assert len(missing_to_queue) == 1
    assert "track three" in normalize_text(missing_to_queue[0]["expected"])
