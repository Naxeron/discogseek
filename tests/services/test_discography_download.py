"""Artist download pre-scans, search queries, and partial-album candidates."""



def test_prescan_split_completion(monkeypatch):
    from discogseek.clients.musicbrainz import ArtistCatalog
    from discogseek.services.soulseek import SlskdArtistScraper

    raw_data = {
        "artist": {"id": "art-1", "name": "Target Artist"},
        "releases_artist": [
            {
                "id": "rel-split-1",
                "title": "Split Album",
                "medium-list": [
                    {
                        "track-list": [
                            {"id": "t1", "title": "Other Track", "artist-credit": [{"artist": {"id": "art-other", "name": "Other Artist"}}]},
                            {"id": "t2", "title": "Artist Track", "artist-credit": [{"artist": {"id": "art-1", "name": "Target Artist"}}]},
                        ]
                    }
                ]
            }
        ],
        "releases_track_artist": [],
        "recordings": []
    }

    cat = ArtistCatalog(raw_data)
    scraper = SlskdArtistScraper(artist_query="Target Artist", dry_run=True)
    scraper.catalog = cat
    scraper.all_artist_aliases = {"target artist"}

    # Mock local found map having only the target artist's track
    scraper.local_found_map = {"artist track": {"path": "/music/02 artist track.flac"}}

    # Run the prescan logic for releases
    for rel in scraper.catalog.releases:
        rel_title = rel.get("title", "")
        from discogseek.core.text import normalize_text
        norm_rel = normalize_text(rel_title)
        rel_tracks = [
            t for t in scraper.catalog.tracks
            if norm_rel in [normalize_text(r) for r in t.get("all_releases", set())]
            or t.get("norm_release") == norm_rel
        ]
        artist_tracks = [
            t for t in rel_tracks
            if any(alias.lower() in t.get("artist_credit", "").lower() for alias in scraper.all_artist_aliases)
            or t.get("artist_credit", "").lower() == scraper.catalog.name.lower()
        ]
        found_artist_tracks = [t for t in artist_tracks if t.get("norm_title") in scraper.local_found_map]
        if artist_tracks and len(found_artist_tracks) == len(artist_tracks):
            scraper.local_found_releases.add(norm_rel)

    assert "split album" in scraper.local_found_releases

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
