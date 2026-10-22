"""Release download workflow: queries, the in-run search cache, transfer history, peer ranking and errors."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from discogseek.clients.slskd import SlskdAPIError, SlskdEnqueueRefusedError
from discogseek.core.peer_policy import PeerPolicy
from discogseek.services import library_download
from discogseek.services.library import LibraryReleaseService
from discogseek.services.library_download import download_missing_tracks


def iso(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def transfer(user, filename, state, transfer_id="old-transfer", **fields):
    return {"username": user, "directories": [{"files": [
        {"id": transfer_id, "filename": filename, "state": state, **fields},
    ]}]}


def response(user, *filenames, **meta):
    files = [name if isinstance(name, dict) else {"filename": name, "size": 1000} for name in filenames]
    return {"username": user, "files": files, **meta}


def client_with(responses=None, by_query=None, downloads=()):
    """Every query returns `responses`, or `by_query[query]` when given; queries are recorded in order."""
    client = MagicMock()
    client.get_downloads.return_value = list(downloads)
    client.browse_directory.return_value = []
    client.queries = []

    def answer(query):
        client.queries.append(query)
        if by_query is not None:
            entry = by_query.get(query, [])
            if isinstance(entry, dict):
                return entry
            return {"responses": entry}
        return {"responses": list(responses or [])}

    client.search.side_effect = lambda query, **kwargs: answer(query)
    client.batch_search.side_effect = lambda queries, **kwargs: {query: answer(query) for query in queries}
    return client


# candidate-matching-7, release-download-8, operational-evidence-6/-11: compilation mode and queries.
def test_featured_credit_does_not_turn_release_into_compilation():
    tracks = [
        {"title": "Miracle Mile", "artist": "Lone feat. Bikôkô", "track_number": 5},
        {"title": "Triton", "artist": "Lone", "track_number": 6},
    ]
    client = client_with([])

    result = download_missing_tracks(client, MagicMock(), "Lone", "Hyperphantasia", tracks, dry_run=True)

    assert client.queries[0] == "Lone Hyperphantasia"
    assert result["artist"] == "Lone"
    assert not any("various artists" in query.casefold() or query.startswith("VA ") for query in client.queries)
    assert "Lone - Miracle Mile" in client.queries
    assert not any("feat" in query.casefold() or "Bikôkô" in query for query in client.queries)


def test_selected_featured_track_searches_primary_artist_first():
    track = {"title": "Miracle Mile", "artist": "Lone feat. Bikôkô", "track_number": 5}
    client = client_with([])

    download_missing_tracks(client, MagicMock(), "Lone", "Hyperphantasia", [track], search_scope="track", dry_run=True)

    assert client.queries[0] == "Lone Miracle Mile"


def test_compilation_searches_bare_title_first_without_va_prefix():
    track = {"title": "Polaris", "artist": "The Yellow Moon Band", "track_number": 10}
    client = client_with([])

    download_missing_tracks(client, MagicMock(), "Various Artists", "Bleep Showcase, Volume 1", [track], dry_run=True)

    # The punctuated title finds the same files as the cleaned one, so it is not searched again.
    assert client.queries[0] == "Bleep Showcase Volume 1"
    assert "Bleep Showcase, Volume 1" not in client.queries
    assert not any(query.startswith(("VA ", "Various Artists")) for query in client.queries)
    assert "The Yellow Moon Band - Polaris" in client.queries


def test_generic_compilation_title_is_qualified_by_credited_artists():
    tracks = [
        {"title": "Friends And Lovers", "artist": "Shigeto", "track_number": 1},
        {"title": "Villain", "artist": "XI", "track_number": 3},
    ]
    unrelated = {"responses": [response("spanish", r"Musica\Cambio de Piel\01 - Otra.mp3")],
                 "state": "Completed, ResponseLimitReached"}
    release = [response("collector", r"Music\VA - Cambio (2012)\01 Shigeto - Friends And Lovers.flac",
                        r"Music\VA - Cambio (2012)\03 XI - Villain.flac")]
    client = client_with(by_query={"Cambio": unrelated, "Cambio XI": release})

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "Cambio", tracks, dry_run=True)

    assert client.queries[:3] == ["Cambio", "Cambio Shigeto", "Cambio XI"]
    assert result["matched_tracks"] == tracks
    assert "Various Artists Cambio" not in client.queries


def test_generic_compilation_falls_back_to_track_and_va_queries():
    tracks = [{"title": "Friends And Lovers", "artist": "Shigeto", "track_number": 1}]
    unrelated = {"responses": [response("spanish", r"Musica\Cambio de Piel\01 - Otra.mp3")],
                 "state": "Completed, ResponseLimitReached"}
    client = client_with(by_query={"Cambio": unrelated})

    download_missing_tracks(client, MagicMock(), "Various Artists", "Cambio", tracks, dry_run=True)

    assert client.queries.index("Cambio Shigeto") < client.queries.index("Various Artists Cambio")
    assert client.queries.index("Various Artists Cambio") < client.queries.index("Shigeto - Friends And Lovers")


@pytest.mark.parametrize("state, qualified", [("Completed, ResponseLimitReached", True), ("Completed", False)])
def test_compilation_title_is_qualified_only_when_its_search_is_truncated(state, qualified):
    track = {"title": "Can We Call It Love", "artist": "Soundspecies", "track_number": 11}
    unrelated = {"responses": [response("peer", r"Music\Other\01 - Bubbles.mp3")], "state": state}
    client = client_with(by_query={"Brownswood Bubblers Four": unrelated})

    download_missing_tracks(client, MagicMock(), "Various Artists", "Brownswood Bubblers Four", [track], dry_run=True)

    assert ("Brownswood Bubblers Four Soundspecies" in client.queries) is qualified
    assert ("Various Artists Brownswood Bubblers Four" in client.queries) is qualified


@pytest.mark.parametrize("scope", ["release", "track"])
def test_title_track_of_a_single_skips_release_title_query(scope):
    track = {"title": "Playhouse", "artist": "Azealia Banks & Lone", "track_number": 1}
    client = client_with([])

    download_missing_tracks(client, MagicMock(), "Azealia Banks & Lone", "Playhouse", [track],
                            search_scope=scope, dry_run=True)

    assert not any(query.casefold().count("playhouse") > 1 for query in client.queries)
    assert any(query.startswith("Azealia Banks") and "Lone" not in query for query in client.queries)


# Query variants that differ only in punctuation find the same files: one search each.
def test_compilation_title_with_punctuation_is_searched_once():
    track = {"title": "Polaris", "artist": "The Yellow Moon Band", "track_number": 10}
    client = client_with([])

    download_missing_tracks(client, MagicMock(), "Various Artists", "Bleep: The Top 100 Tracks of 2009", [track],
                            dry_run=True)

    assert client.queries[0] == "Bleep The Top 100 Tracks of 2009"
    assert "Bleep: The Top 100 Tracks of 2009" not in client.queries


def test_track_query_with_brackets_is_searched_once():
    track = {"title": "Midnight Feast (Bibio remix)", "artist": "Lone", "track_number": 3}
    client = client_with([])

    download_missing_tracks(client, MagicMock(), "Lone", "Joyreel / Sunset Teens", [track], search_scope="track",
                            dry_run=True)

    assert client.queries[0] == "Lone Midnight Feast Bibio remix"
    assert "Lone Midnight Feast (Bibio remix)" not in client.queries
    assert "Lone Joyreel / Sunset Teens" not in client.queries
    assert len({query.casefold() for query in client.queries}) == len(client.queries)


# candidate-matching-13, operational-evidence-12: an in-run cache shared by every phase and call.
def test_search_cache_issues_each_query_once_across_calls():
    tracks = [{"title": "Big World", "artist": "Lone feat. Lou Hayter", "track_number": 5}]
    client = client_with([])
    cache = {}

    download_missing_tracks(client, MagicMock(), "Lone", "Emerald Fantasy Tracks", tracks,
                            dry_run=True, search_cache=cache)
    first = list(client.queries)
    download_missing_tracks(client, MagicMock(), "Lone", "Emerald Fantasy Tracks", tracks,
                            dry_run=True, search_cache=cache)

    assert first and client.queries == first
    assert len(client.queries) == len(set(query.casefold() for query in client.queries))


def test_album_and_track_phases_share_results_within_a_call():
    track = {"title": "Restless City", "artist": "Lone", "track_number": 1}
    client = client_with([])

    download_missing_tracks(client, MagicMock(), "Lone", "Restless City", [track], search_scope="track", dry_run=True)

    assert client.queries.count("Lone Restless City") == 1


def test_failed_searches_are_not_cached():
    tracks = [{"title": "Big World", "artist": "Lone", "track_number": 5}]
    client = client_with([])
    client.search.side_effect = SlskdAPIError("slskd busy (HTTP 429)")
    cache = {}

    download_missing_tracks(client, MagicMock(), "Lone", "Emerald Fantasy Tracks", tracks,
                            dry_run=True, search_cache=cache)

    assert "lone emerald fantasy tracks" not in cache


def test_album_query_reuses_the_runs_search_without_punctuation():
    """Artist mode's discovery searched 'Lone Joyreel Sunset Teens' earlier in the run."""
    track = {"title": "Sunset Teens", "artist": "Lone", "track_number": 2}
    found = {"responses": [response("peer", r"Music\Lone\Joyreel - Sunset Teens\02 Lone - Sunset Teens.flac")]}
    cache = {"lone joyreel sunset teens": found}
    client = client_with([])

    result = download_missing_tracks(client, MagicMock(), "Lone", "Joyreel / Sunset Teens", [track], dry_run=True,
                                     search_cache=cache)

    assert client.queries == []
    assert result["matched_count"] == 1


@pytest.mark.parametrize("scope", ["release", "track"])
def test_use_existing_false_reaches_every_search(scope):
    client = client_with([])
    tracks = [{"title": "Miracle Mile", "artist": "Lone", "track_number": 5},
              {"title": "Triton", "artist": "Lone", "track_number": 6}]

    download_missing_tracks(client, MagicMock(), "Lone", "Hyperphantasia", tracks, search_scope=scope,
                            dry_run=True, use_existing=False)

    calls = client.search.call_args_list + client.batch_search.call_args_list
    assert client.search.called and client.batch_search.called
    assert all(call.kwargs.get("use_existing") is False for call in calls)


def test_searches_reuse_slskd_history_by_default():
    client = client_with([])

    download_missing_tracks(client, MagicMock(), "Lone", "Hyperphantasia",
                            [{"title": "Miracle Mile", "artist": "Lone", "track_number": 5}], dry_run=True)

    calls = client.search.call_args_list + client.batch_search.call_args_list
    assert calls and not any("use_existing" in call.kwargs for call in calls)


def test_library_service_forwards_official_tracks_and_cache(monkeypatch):
    captured = {}
    monkeypatch.setattr(library_download, "download_missing_tracks", lambda **kwargs: captured.update(kwargs) or {})
    service = LibraryReleaseService(mb_client=MagicMock(), slskd_client=MagicMock())
    official = [{"title": "Song", "track_number": "1"}]
    cache = {}

    service.download_missing_tracks("Artist", "Album", official, official_tracks=official, search_cache=cache)

    assert captured["official_tracks"] is official
    assert captured["search_cache"] is cache


def test_library_service_forwards_search_and_downloads_options(monkeypatch):
    captured = []
    monkeypatch.setattr(library_download, "download_missing_tracks", lambda **kwargs: captured.append(kwargs) or {})
    service = LibraryReleaseService(mb_client=MagicMock(), slskd_client=MagicMock())
    rows = [{"title": "Song", "track_number": "1"}]

    service.download_missing_tracks("Artist", "Album", rows, use_existing=False, downloads_dir="/srv/slskd")
    service.download_missing_tracks("Artist", "Album", rows)

    assert captured[0]["use_existing"] is False and captured[0]["downloads_dir"] == "/srv/slskd"
    assert "use_existing" not in captured[1] and "downloads_dir" not in captured[1]


# candidate-matching-A3, release-download-1, release-download-7: transfer history.
def test_history_reuses_featured_credit_transfer_without_duplicate():
    filename = r"Music\Lone - Emerald Fantasy Tracks\05 - Big World.flac"
    track = {"title": "Big World", "artist": "Lone feat. Lou Hayter", "track_number": 5}
    client = client_with([response("peerB", r"B\Lone - Emerald Fantasy Tracks\05 - Big World.flac")],
                         downloads=[transfer("peerA", filename, "Queued, Remotely")])

    result = download_missing_tracks(client, MagicMock(), "Lone", "Emerald Fantasy Tracks", [track])

    client.enqueue_download.assert_not_called()
    assert [(file["user"], file["filename"]) for file in result["queued_files"]] == [("peerA", filename)]


@pytest.mark.parametrize("ended", [{"days": 12}, {"minutes": 10}])
def test_succeeded_transfer_from_another_release_does_not_satisfy_row(ended):
    single = r"music\Lone\Lone - Waterfall Reverse\Lone - Waterfall Reverse - 01 Waterfall Reverse.flac"
    album = r"Music\Lone\Hyperphantasia\11 Lone - Waterfall Reverse.flac"
    track = {"title": "Waterfall Reverse", "artist": "Lone", "track_number": 11}
    client = client_with([response("album-peer", album)],
                         downloads=[transfer("netgf", single, "Completed, Succeeded", endedAt=iso(**ended))])

    result = download_missing_tracks(client, MagicMock(), "Lone", "Hyperphantasia", [track])

    client.enqueue_download.assert_called_once()
    assert client.enqueue_download.call_args.args[0] == "album-peer"
    assert result["enqueued_files"] == result["queued_files"]
    assert result["queued_files"][0]["filename"] == album


def test_succeeded_other_release_file_in_results_does_not_satisfy_row():
    """The search response spells the peer differently from its transfer history."""
    single = r"music\Lone\Lone - Waterfall Reverse\Lone - Waterfall Reverse - 01 Waterfall Reverse.flac"
    album = r"Music\Lone\Hyperphantasia\11 Lone - Waterfall Reverse.flac"
    track = {"title": "Waterfall Reverse", "artist": "Lone", "track_number": 11}
    client = client_with([response("netgf", single), response("album-peer", album)],
                         downloads=[transfer("NetGF", single, "Completed, Succeeded", endedAt=iso(minutes=10))])

    result = download_missing_tracks(client, MagicMock(), "Lone", "Hyperphantasia", [track])

    assert [file["user"] for file in result["enqueued_files"]] == ["album-peer"]


def test_recent_succeeded_transfer_in_release_folder_is_still_protected():
    filename = r"music\Lone\Hyperphantasia\11 Lone - Waterfall Reverse.flac"
    track = {"title": "Waterfall Reverse", "artist": "Lone", "track_number": 11}
    client = client_with([response("other", r"Other\Lone\Hyperphantasia\11 Lone - Waterfall Reverse.flac")],
                         downloads=[transfer("netgf", filename, "Completed, Succeeded", endedAt=iso(minutes=10))])

    result = download_missing_tracks(client, MagicMock(), "Lone", "Hyperphantasia", [track])

    client.enqueue_download.assert_not_called()
    assert result["queued_tracks"] == [track]


def test_queued_decorated_transfer_protects_its_row():
    filename = r"Music\Lone\2020 - The Light Year Wave Tape [Album]\07 - Cody [FLAC] [889 kbps].flac"
    track = {"title": "Cody", "artist": "Lone", "track_number": 7}
    client = client_with([], downloads=[transfer("gliczide", filename, "Queued, Remotely")])

    result = download_missing_tracks(client, MagicMock(), "Lone", "The Light Year Wave Tape", [track])

    assert result["queued_tracks"] == [track]
    assert client.queries == []
    client.enqueue_download.assert_not_called()


# operational-evidence-2: a transfer stuck in a remote queue is replaced and cancelled.
STALE_FILE = r"Music\Lone\Once in a While\03 - Raptured.flac"
ROW = {"title": "Raptured", "artist": "Lone", "track_number": 3}


def stale_history(age):
    return [transfer("seiko", STALE_FILE, "Queued, Remotely", transfer_id="stale-id",
                     enqueuedAt=iso(**age), requestedAt=iso(**age))]


@pytest.mark.parametrize("seiko_in_results", [False, True])
def test_stale_queued_transfer_is_cancelled_when_its_row_is_replaced(seiko_in_results):
    responses = [response("healthy", r"Share\Lone\Once in a While\03 - Raptured.flac")]
    if seiko_in_results:
        responses.insert(0, response("seiko", STALE_FILE))
    client = client_with(responses, downloads=stale_history({"days": 12}))
    operations = []
    client.cancel_download.side_effect = lambda *args: operations.append(("cancel", args))
    client.enqueue_download.side_effect = lambda user, files: operations.append(("enqueue", user))

    result = download_missing_tracks(client, MagicMock(), "Lone", "Once in a While", [dict(ROW)])

    assert operations == [("enqueue", "healthy"), ("cancel", ("seiko", "stale-id"))]
    assert result["queued_files"][0]["user"] == "healthy"
    assert result["queue_errors"] == []


def test_recent_queued_transfer_stays_protected():
    client = client_with([response("healthy", r"Share\Lone\Once in a While\03 - Raptured.flac")],
                         downloads=stale_history({"minutes": 30}))

    result = download_missing_tracks(client, MagicMock(), "Lone", "Once in a While", [dict(ROW)])

    assert result["queued_files"][0]["user"] == "seiko"
    client.cancel_download.assert_not_called()
    client.enqueue_download.assert_not_called()


def test_stale_transfer_is_cancelled_only_for_the_replaced_row():
    other_file = r"Music\Lone\Once in a While\04 - Once in a While.flac"
    rows = [dict(ROW), {"title": "Once in a While", "artist": "Lone", "track_number": 4}]
    downloads = stale_history({"days": 12}) + [
        transfer("seiko", other_file, "Queued, Remotely", transfer_id="other-stale",
                 enqueuedAt=iso(days=12), requestedAt=iso(days=12)),
    ]
    client = client_with([response("healthy", r"Share\Lone\Once in a While\03 - Raptured.flac")], downloads=downloads)

    result = download_missing_tracks(client, MagicMock(), "Lone", "Once in a While", rows)

    client.cancel_download.assert_called_once_with("seiko", "stale-id")
    assert result["queued_tracks"] == [rows[0]]
    errors = [error for error in result["queue_errors"] if error.get("stage") == "recovery"]
    assert len(errors) == 1 and errors[0]["tracks"] == [rows[1]]
    assert "No alternative sources" in errors[0]["error"] and "waiting for 12 days" in errors[0]["error"]


def test_dry_run_never_cancels_stale_transfer():
    client = client_with([response("healthy", r"Share\Lone\Once in a While\03 - Raptured.flac")],
                         downloads=stale_history({"days": 12}))

    result = download_missing_tracks(client, MagicMock(), "Lone", "Once in a While", [dict(ROW)], dry_run=True)

    assert result["queued_files"][0]["user"] == "healthy"
    client.cancel_download.assert_not_called()


# A second, stale copy of a row that a live transfer keeps (a cancel that failed earlier,
# another tool's request) is stopped too, even when it ranks after the live one.
KEPT_FILE = r"Share\Lone\Once in a While\03 - Raptured.flac"


def kept_and_stale_history():
    return [transfer("healthy", KEPT_FILE, "Queued, Remotely", transfer_id="kept-id", enqueuedAt=iso(minutes=30)),
            *stale_history({"days": 10})]


@pytest.mark.parametrize("dry_run", [False, True])
def test_stale_duplicate_of_a_kept_transfer_is_cancelled(dry_run):
    client = client_with([], downloads=kept_and_stale_history())

    result = download_missing_tracks(client, MagicMock(), "Lone", "Once in a While", [dict(ROW)], dry_run=dry_run)

    assert [(file["user"], file["filename"]) for file in result["queued_files"]] == [("healthy", KEPT_FILE)]
    assert client.queries == []
    client.enqueue_download.assert_not_called()
    if dry_run:
        client.cancel_download.assert_not_called()
    else:
        client.cancel_download.assert_called_once_with("seiko", "stale-id")
        assert result["queue_errors"] == []


def test_failed_cancel_of_a_stale_duplicate_is_reported():
    client = client_with([], downloads=kept_and_stale_history())
    client.cancel_download.side_effect = SlskdAPIError("HTTP 500")

    result = download_missing_tracks(client, MagicMock(), "Lone", "Once in a While", [dict(ROW)])

    assert result["queued_tracks"] == [ROW]
    assert [error["stage"] for error in result["queue_errors"]] == ["recovery"]
    assert "Kept a queued transfer" in result["queue_errors"][0]["error"]


# A compilation's own folder rarely names each track's artist; it still identifies its transfers.
VA_ROW = {"title": "Polaris", "artist": "The Yellow Moon Band", "track_number": 10}
VA_QUEUED = r"Music\Compilations\Bleep Showcase Volume 1\10 - Polaris.mp3"
VA_ALTERNATIVE = r"Share\Bleep Showcase Volume 1\10 - Polaris.flac"


def test_live_compilation_transfer_protects_its_row_without_the_artist_in_its_path():
    client = client_with([response("other", VA_ALTERNATIVE)],
                         downloads=[transfer("queued", VA_QUEUED, "Queued, Remotely", enqueuedAt=iso(minutes=20))])

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "Bleep Showcase, Volume 1", [dict(VA_ROW)])

    client.enqueue_download.assert_not_called()
    assert [(file["user"], file["filename"]) for file in result["queued_files"]] == [("queued", VA_QUEUED)]


def test_stale_compilation_transfer_is_cancelled_when_its_row_is_replaced():
    client = client_with([response("other", VA_ALTERNATIVE)], downloads=[
        transfer("queued", VA_QUEUED, "Queued, Remotely", transfer_id="stale-id",
                 enqueuedAt=iso(days=3), requestedAt=iso(days=3)),
    ])
    operations = []
    client.cancel_download.side_effect = lambda *args: operations.append(("cancel", args))
    client.enqueue_download.side_effect = lambda user, files: operations.append(("enqueue", user))

    download_missing_tracks(client, MagicMock(), "Various Artists", "Bleep Showcase, Volume 1", [dict(VA_ROW)])

    assert operations == [("enqueue", "other"), ("cancel", ("queued", "stale-id"))]


# release-download-9, operational-evidence-A1: free slot, short queue and speed after the format.
BUSY = {"hasFreeUploadSlot": False, "queueLength": 900, "uploadSpeed": 100_000}
IDLE = {"hasFreeUploadSlot": True, "queueLength": 0, "uploadSpeed": 9_000_000}


@pytest.mark.parametrize("mode", ["release", "track", "complete"])
def test_identical_sources_prefer_free_slot_and_short_queue(mode):
    track = {"title": "Miracle Mile", "artist": "Lone", "track_number": 5}
    filename = r"Music\Lone\Hyperphantasia\05 - Miracle Mile.flac"
    client = client_with([response("a-busy", filename, **BUSY), response("z-idle", filename, **IDLE)])
    kwargs = {"complete_release_tracks": [track]} if mode == "complete" else {"search_scope": mode}

    result = download_missing_tracks(client, MagicMock(), "Lone", "Hyperphantasia", [track], dry_run=True, **kwargs)

    assert result["queued_files"][0]["user"] == "z-idle"


@pytest.mark.parametrize("scope", ["release", "track"])
def test_preferred_format_outranks_peer_health(scope):
    track = {"title": "Miracle Mile", "artist": "Lone", "track_number": 5}
    client = client_with([
        response("busy-flac", r"Music\Lone\Hyperphantasia\05 - Miracle Mile.flac", **BUSY),
        response("idle-mp3", {"filename": r"Music\Lone\Hyperphantasia\05 - Miracle Mile.mp3", "bitRate": 320}, **IDLE),
    ])

    result = download_missing_tracks(client, MagicMock(), "Lone", "Hyperphantasia", [track],
                                     search_scope=scope, dry_run=True)

    assert result["queued_files"][0]["user"] == "busy-flac"


@pytest.mark.parametrize("mode", ["release", "track", "complete"])
def test_whole_release_comes_from_the_healthy_peer(mode):
    tracks = [{"title": title, "artist": "Lone", "track_number": number}
              for number, title in enumerate(["Opening", "Middle", "Closing"], 1)]
    folder = "Music\\Lone\\In Order To Care\\"
    files = [f"{folder}{track['track_number']:02d} - {track['title']}.flac" for track in tracks]
    client = client_with([
        response("darkling23", *files, hasFreeUploadSlot=False, queueLength=347, uploadSpeed=328_000),
        response("siick", *files, hasFreeUploadSlot=True, queueLength=0, uploadSpeed=4_600_000),
    ])
    kwargs = {"complete_release_tracks": tracks} if mode == "complete" else {"search_scope": mode}

    result = download_missing_tracks(client, MagicMock(), "Lone", "In Order To Care", tracks, dry_run=True, **kwargs)

    assert {file["user"] for file in result["queued_files"]} == {"siick"}
    assert result["matched_count"] == 3


# release-download-13: retryable search errors and recovery messages.
def test_album_search_failure_is_dropped_after_track_fallback_queues_row():
    track = {"title": "Miracle Mile", "artist": "Lone", "track_number": 5}
    good = {"responses": [response("peer", r"Music\Lone\Hyperphantasia\05 - Miracle Mile.flac")]}
    client = client_with([])
    client.search.side_effect = SlskdAPIError("slskd busy (HTTP 429)")
    client.batch_search.side_effect = lambda queries, **kwargs: {
        query: good if "Miracle" in query else {"error": "slskd busy (HTTP 429)", "responses": [], "searchText": query}
        for query in queries
    }

    result = download_missing_tracks(client, MagicMock(), "Lone", "Hyperphantasia", [track])

    assert result["queued_count"] == 1
    assert result["queue_errors"] == []


def test_batch_search_error_entries_are_retryable_search_errors():
    track = {"title": "Miracle Mile", "artist": "Lone", "track_number": 5}
    client = client_with([])
    client.search.side_effect = lambda query, **kwargs: {"responses": []}
    client.batch_search.side_effect = lambda queries, **kwargs: {
        query: {"error": "HTTP 409 Conflict", "responses": [], "searchText": query} for query in queries
    }

    result = download_missing_tracks(client, MagicMock(), "Lone", "Hyperphantasia", [track])

    assert result["queued_count"] == 0
    assert result["queue_errors"] == [{"stage": "search", "error": "HTTP 409 Conflict", "tracks": [track]}]
    # A failed round switches strategy instead of trying every remaining query variant.
    assert client.batch_search.call_count == 1


def test_blocked_peer_alone_is_not_reported_as_failed_recovery():
    PeerPolicy().observe([transfer("Blocked", "Unrelated/Old.flac", "Completed, Rejected", exception="Banned")])
    track = {"title": "Pulsewidth", "artist": "Artist", "track_number": 3}
    client = client_with([response("Blocked", r"Artist\Album\03 Pulsewidth.flac")])

    result = download_missing_tracks(client, MagicMock(), "Artist", "Album", [track])

    assert result["queued_count"] == 0
    assert not any("No alternative sources" in error["error"] for error in result["queue_errors"])


# Peer folders that searches showed in part are read whole.
def test_compilation_folder_is_browsed_for_remaining_tracks():
    tracks = [
        {"title": "Friends And Lovers", "artist": "Shigeto", "track_number": 1},
        {"title": "Villain", "artist": "XI", "track_number": 3},
    ]
    folder = r"Music\VA - Cambio (2012)"
    client = client_with(by_query={
        "Cambio Shigeto": [response("collector", folder + r"\01 Shigeto - Friends And Lovers.flac")],
    })
    client.browse_directory.return_value = [
        {"filename": folder + r"\01 Shigeto - Friends And Lovers.flac", "size": 1000},
        {"filename": folder + r"\03 XI - Villain.flac", "size": 1000},
    ]

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "Cambio", tracks, dry_run=True)

    client.browse_directory.assert_called_once_with("collector", folder)
    assert result["matched_tracks"] == tracks
    assert {file["user"] for file in result["queued_files"]} == {"collector"}


def test_release_numbering_outranks_a_healthier_box_set_copy():
    """Transmissions Sampler: the sampler's own folder beats the box set's disc 3 of the same recordings."""
    titles = [(1, "Incidental Harmony", 521), (2, "Sublime Creation", 714), (3, "Epsilon Phase", 509)]
    tracks = [{"title": title, "artist": "Global Communication", "track_number": number, "duration": float(length)}
              for number, title, length in titles]
    sampler = [{"filename": f"Artists\\Global Communication\\2020 - Transmissions Sampler\\{number}. {title}.flac",
                "size": 1000, "length": length} for number, title, length in titles]
    box_set = [{"filename": f"GLOBAL COMMUNICATION\\Transmissions (2020)\\30{number} - {title}.flac",
                "size": 1000, "length": length} for number, title, length in titles]
    client = client_with([response("box", *box_set, **IDLE), response("sampler", *sampler, **BUSY)])

    result = download_missing_tracks(client, MagicMock(), "Global Communication", "Transmissions Sampler", tracks,
                                     dry_run=True)

    assert {file["user"] for file in result["queued_files"]} == {"sampler"}


@pytest.mark.parametrize("scope", ["release", "track"])
def test_refused_file_is_replaced_by_another_peer(scope):
    """slskd's partial-failure reply names the refused file; its row is tried again elsewhere."""
    track = {"title": "Miracle Mile", "artist": "Lone", "track_number": 5}
    client = client_with([
        response("refusing", r"Music\Lone\Hyperphantasia\05 - Miracle Mile.flac"),
        response("serving", {"filename": r"Music\Lone\Hyperphantasia\05 - Miracle Mile.mp3", "bitRate": 320}),
    ])

    def enqueue(user, files):
        if user == "refusing":
            raise SlskdEnqueueRefusedError("slskd did not queue 1 of 1 files from refusing: File not shared",
                                           queued_files=[])

    client.enqueue_download.side_effect = enqueue

    result = download_missing_tracks(client, MagicMock(), "Lone", "Hyperphantasia", [track], search_scope=scope)

    assert [call.args[0] for call in client.enqueue_download.call_args_list] == ["refusing", "serving"]
    assert result["queued_tracks"] == [track]
    assert result["queue_errors"] == []
