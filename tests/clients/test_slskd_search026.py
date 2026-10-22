"""SlskdClient searches against slskd 0.26 semantics (tests/helpers/slskd026.py)."""

import logging
from unittest.mock import MagicMock

import pytest

from discogseek.clients.slskd import SlskdAPIError, SlskdClient
from tests.helpers.slskd026 import FakeSlskd026, peer

DAY = 86400.0
HYPERPHANTASIA = "Music\\Lone\\Hyperphantasia (2024) [FLAC]"


@pytest.fixture
def slskd(monkeypatch):
    monkeypatch.setattr("discogseek.clients.slskd.SEARCH_REUSE_MAX_AGE", 3600.0)
    client = SlskdClient(base_url="http://mock:5030", api_key="dummy_key")
    fake = FakeSlskd026().install(monkeypatch, client)
    return client, fake


def hyperphantasia_responses(count=134):
    # The live 'Hyperphantasia' search ran 33.4 s: its responses kept arriving
    # for 18.4 s, then the 15 s inactivity timer completed it.
    responses = [peer("araiguma", *(f"{HYPERPHANTASIA}\\{n:02d} Lone - Track {n}.flac" for n in range(1, 15)))]
    responses += [peer(f"peer{n}", f"Music\\Lone\\Hyperphantasia\\{n % 14 + 1:02d} track.mp3") for n in range(count - 1)]
    arrivals = [18.4 * n / (count - 1) for n in range(count)]
    return responses, arrivals


def run(client, method, query, **kwargs):
    if method == "search":
        return client.search(query, **kwargs)
    return client.batch_search([query], **kwargs)[query]


def posts(fake):
    return [body["searchText"] for _, _, body in fake.calls("POST", "/api/v0/searches")]


@pytest.mark.parametrize("method", ["search", "batch_search"])
def test_long_productive_search_returns_every_response(slskd, method):
    client, fake = slskd
    responses, arrivals = hyperphantasia_responses()
    fake.plan_search("Hyperphantasia", responses, arrivals)

    result = run(client, method, "Hyperphantasia", timeout=30)

    assert result["state"] == "Completed, TimedOut"
    assert len(result["responses"]) == 134
    assert result["responses"][0]["username"] == "araiguma"
    assert 33.4 < fake.now < 36
    assert fake.calls("PUT") == []


def test_batch_search_returns_every_long_search_of_the_batch(slskd):
    client, fake = slskd
    responses, arrivals = hyperphantasia_responses()
    fake.plan_search("Hyperphantasia", responses, arrivals)
    fake.plan_search("VA Hyperphantasia", responses[:3], [1.0, 2.0, 3.0])

    results = client.batch_search(["Hyperphantasia", "VA Hyperphantasia"], timeout=30)

    assert {query: len(result["responses"]) for query, result in results.items()} == {
        "Hyperphantasia": 134, "VA Hyperphantasia": 3,
    }


@pytest.mark.parametrize("method", ["search", "batch_search"])
def test_search_still_running_at_time_limit_is_stopped_and_keeps_its_responses(slskd, method, caplog):
    client, fake = slskd
    caplog.set_level(logging.INFO, logger="discogseek.clients.slskd")
    # A broad query whose peers keep answering never reaches the inactivity timeout.
    fake.plan_search("Lone", [peer(f"peer{n}", f"Music\\Lone\\{n:03d}.flac") for n in range(400)],
                     [float(n) for n in range(400)])

    result = run(client, method, "Lone", timeout=25)

    stops = fake.calls("PUT")
    assert len(stops) == 1 and stops[0][1].startswith("/api/v0/searches/")
    assert result["state"] == "Completed, Cancelled"
    assert len(result["responses"]) == 74  # every response received by the 73 s limit
    assert "still running after 73s; stopping it to keep its results — Lone" in caplog.text


@pytest.mark.parametrize("method", ["search", "batch_search"])
def test_search_whose_responses_are_never_stored_is_a_failure(slskd, method):
    client, fake = slskd
    # slskd restarted moments ago: the row looks live but will never finish.
    fake.add_history("Lone Temples", state="InProgress", age=5, response_count=64, file_count=300)

    if method == "search":
        with pytest.raises(SlskdAPIError, match="did not finish the search"):
            client.search("Lone Temples", timeout=25)
    else:
        result = client.batch_search(["Lone Temples"], timeout=25)["Lone Temples"]
        assert result == {
            "error": "slskd did not finish the search within 78s",
            "responses": [], "searchText": "Lone Temples",
        }
    assert len(fake.calls("PUT")) == 1
    assert posts(fake) == []


@pytest.mark.parametrize("method", ["search", "batch_search"])
def test_errored_search_without_responses_is_a_failure(slskd, method):
    client, fake = slskd
    # The Soulseek server connection dropped 2 s into the search.
    fake.errored_searches["lone cody"] = 2.0
    fake.plan_search("Lone Cody", [peer("peer", "Music\\Lone\\Cody.flac")], [5.0])

    if method == "search":
        with pytest.raises(SlskdAPIError, match="errored before any responses"):
            client.search("Lone Cody")
    else:
        result = client.batch_search(["Lone Cody"])["Lone Cody"]
        assert result["responses"] == [] and "errored before any responses" in result["error"]


@pytest.mark.parametrize("method", ["search", "batch_search"])
def test_errored_search_keeps_the_responses_it_received(slskd, method):
    client, fake = slskd
    fake.errored_searches["lone cody"] = 4.0
    fake.plan_search("Lone Cody", [peer("early", "Music\\Lone\\Cody.flac"), peer("late", "Music\\Cody.flac")],
                     [1.0, 6.0])

    result = run(client, method, "Lone Cody")

    assert result["state"] == "Completed, Errored"
    assert [response["username"] for response in result["responses"]] == ["early"]


@pytest.mark.parametrize("method", ["search", "batch_search"])
def test_search_stopped_while_queued_is_a_failure(slskd, method):
    client, fake = slskd
    # Other clients hold both slskd search slots for the whole time.
    fake.queue_new_searches = True
    fake.plan_search("Lone Cody", [peer("peer", "Music\\Lone\\Cody.flac")])

    if method == "search":
        with pytest.raises(SlskdAPIError, match="stopped before any responses"):
            client.search("Lone Cody", timeout=25)
    else:
        result = client.batch_search(["Lone Cody"], timeout=25)["Lone Cody"]
        assert result["responses"] == [] and "stopped before any responses" in result["error"]
    # The time limit counts from the slot start, up to twice the limit overall.
    assert [time for time, verb, _ in fake.request_times if verb == "PUT"] == [146]


@pytest.mark.parametrize("method", ["search", "batch_search"])
def test_completed_search_is_reread_until_slskd_stores_its_responses(monkeypatch, method):
    client = SlskdClient(base_url="http://mock:5030", api_key="dummy_key")
    fake = FakeSlskd026(finalize_lag=3.0).install(monkeypatch, client)
    fake.plan_search("Urban Biology", [peer("ludux1", "@@txkzu\\n36\\a20\\Machinedrum 3 (2002-09) Urban Biology\\03 Machinedrum - Icya.mp3")])

    result = run(client, method, "Urban Biology")

    assert result["isComplete"] is True
    assert result["endedAt"]
    assert [response["username"] for response in result["responses"]] == ["ludux1"]


def test_batch_search_keeps_two_searches_running_with_a_sliding_window(slskd):
    client, fake = slskd
    # One slow search must not hold back the queries queued behind the other slot.
    fake.plan_search("Lone Lemurian", [peer("slow", "Music\\Lone\\Lemurian\\01 Lemurian.flac")] * 46,
                     [float(n) for n in range(46)])
    queries = ["Lone Lemurian", "Lone Temples", "Lone Crystal Caverns 1991", "Lone Coreshine Voodoo", "Lone Hyper Seconds"]
    for query in queries[1:]:
        fake.plan_search(query, [peer("quick", f"Music\\Lone\\{query}.flac")], [1.0])

    results = client.batch_search(queries, timeout=30)

    assert fake.peak_running == 2
    assert all(len(results[query]["responses"]) >= 1 for query in queries)
    # Chunked pairs would need 61 + 17 + 17 s; the window overlaps the slow search.
    assert fake.now == 68
    post_times = {}
    for method, path, body in fake.requests:
        if method == "POST":
            post_times[body["searchText"]] = len(post_times)
    assert list(post_times) == queries


def test_batch_search_times_an_attached_search_from_its_own_start(slskd):
    client, fake = slskd
    for query in ("Lone Temples", "Lone Chroma"):
        fake.plan_search(query, [peer("quick", f"Music\\Lone\\{query}.flac")], [1.0])
    # Another client's broad search started 10 s before ours and never goes quiet.
    fake.add_running("Lone", age=10, responses=[peer(f"peer{n}", f"Music\\Lone\\{n:03d}.flac") for n in range(200)],
                     arrivals=[float(n) for n in range(200)])

    results = client.batch_search(["Lone Temples", "Lone Chroma", "Lone"], timeout=25)

    # It joins the window at 17 s, 27 s after it started; the 73 s limit ends at 63 s.
    assert [time for time, method, _ in fake.request_times if method == "PUT"] == [63]
    assert results["Lone"]["state"] == "Completed, Cancelled"
    assert len(results["Lone"]["responses"]) == 74


def test_batch_search_reports_failed_submissions_next_to_successful_searches(slskd, caplog):
    client, fake = slskd
    fake.plan_search("Lone Cody", [peer("peer", "Music\\Lone\\Cody.flac")])
    # Soulseek server connection lost: slskd answers 409 Conflict.
    fake.search_post_errors["lone epiphany"] = [
        (409, "The server connection must be connected and logged in to perform a search")
    ]

    results = client.batch_search(["Lone Cody", "Lone Epiphany"])

    assert len(results["Lone Cody"]["responses"]) == 1
    assert "error" not in results["Lone Cody"]
    failed = results["Lone Epiphany"]
    assert failed["responses"] == [] and failed["searchText"] == "Lone Epiphany"
    assert "HTTP 409" in failed["error"] and "must be connected" in failed["error"]


def test_search_retries_when_slskd_handles_another_search_request(slskd):
    client, fake = slskd
    fake.busy_search_posts = 2
    fake.plan_search("Lone Begin to Begin", [peer("peer", "Music\\Lone\\Begin to Begin.flac")])

    result = client.search("Lone Begin to Begin")

    assert len(result["responses"]) == 1
    assert len(fake.calls("POST", "/api/v0/searches")) == 3


@pytest.mark.parametrize("method", ["search", "batch_search"])
def test_deleted_search_is_reported_as_a_failure(slskd, method):
    client, fake = slskd
    fake.plan_search("Lone Chroma", [peer("peer", "Music\\Lone\\Chroma.flac")], [30.0])
    original = fake.request

    def delete_while_running(verb, path, **kwargs):
        if verb == "GET" and "includeResponses" in path and fake.now >= 3:
            fake.searches.clear()
        return original(verb, path, **kwargs)

    client._request = delete_while_running
    if method == "search":
        with pytest.raises(SlskdAPIError, match="has no search"):
            client.search("Lone Chroma")
    else:
        result = client.batch_search(["Lone Chroma"])["Lone Chroma"]
        assert result["responses"] == [] and "has no search" in result["error"]
    assert fake.now == 3


def test_time_limit_starts_when_slskd_starts_a_queued_search(monkeypatch):
    # Other clients' searches can hold both slskd slots; ours waits as Queued.
    client = SlskdClient(base_url="http://mock:5030", api_key="dummy_key")
    clock = {"now": 0.0}
    monkeypatch.setattr("discogseek.clients.slskd.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr("discogseek.clients.slskd.time.sleep", lambda seconds: clock.update(now=clock["now"] + seconds))
    monkeypatch.setattr(client, "list_searches", lambda: [])

    def respond(method, path, **kwargs):
        response = MagicMock(status_code=200)
        if method == "POST":
            response.json.return_value = {"id": "queued", "state": "Requested"}
        elif clock["now"] < 60:
            response.json.return_value = {"isComplete": False, "state": "Queued", "responses": []}
        elif clock["now"] < 120:
            response.json.return_value = {"isComplete": False, "state": "InProgress", "responseCount": 3, "responses": []}
        else:
            response.json.return_value = {"isComplete": True, "state": "Completed, TimedOut", "responseCount": 1,
                                          "responses": [peer("peer", "Music\\Lone\\Airglow Fires.flac")]}
        return response

    request = MagicMock(side_effect=respond)
    monkeypatch.setattr(client, "_request", request)

    result = client.search("Lone Airglow Fires", timeout=25)

    assert len(result["responses"]) == 1
    assert clock["now"] == 120
    assert all(entry.args[0] != "PUT" for entry in request.call_args_list)


class TestHistoryReuse:
    """Only recent, normally completed searches are reused; zombies never capture a query."""

    @pytest.mark.parametrize("method", ["search", "batch_search"])
    def test_abandoned_in_progress_search_is_replaced_and_deleted(self, slskd, method):
        client, fake = slskd
        zombie = fake.add_history("Alarma", state="InProgress", age=13 * DAY, response_count=248, file_count=1149)
        fake.plan_search("Alarma", [peer("peer", "Music\\Alarma\\01 Alarma.flac")])

        result = run(client, method, "Alarma")

        assert posts(fake) == ["Alarma"]
        assert len(result["responses"]) == 1
        assert ("DELETE", f"/api/v0/searches/{zombie}", None) in fake.requests
        assert fake.now < 20

    @pytest.mark.parametrize("method", ["search", "batch_search"])
    def test_overdue_running_search_is_ignored_but_kept(self, slskd, method):
        client, fake = slskd
        # Older than the time limit, but possibly another client's long search.
        fake.add_history("VA Children of Mu", state="InProgress", age=120, response_count=186)
        fake.plan_search("VA Children of Mu", [peer("peer", "Music\\VA\\Children of Mu\\01.flac")])

        result = run(client, method, "VA Children of Mu")

        assert posts(fake) == ["VA Children of Mu"]
        assert len(result["responses"]) == 1
        assert fake.calls("DELETE") == []

    @pytest.mark.parametrize("method", ["search", "batch_search"])
    def test_recent_running_search_is_attached(self, slskd, method):
        client, fake = slskd
        fake.add_running("Lone Temples", age=10, responses=[peer("peer", "Music\\Lone\\Temples\\01 Temples.flac")],
                         arrivals=[12.0])

        result = run(client, method, "Lone Temples")

        assert posts(fake) == []
        assert len(result["responses"]) == 1
        assert 17 <= fake.now <= 19

    @pytest.mark.parametrize("method", ["search", "batch_search"])
    @pytest.mark.parametrize("state, age, reused", [
        ("Completed, TimedOut", 5 * 60, True),
        ("Completed, ResponseLimitReached", 30 * 60, True),
        ("Completed, FileLimitReached", 30 * 60, True),
        ("Completed, TimedOut", 12 * DAY, False),
        ("Completed, TimedOut", 2 * 3600, False),
        ("Completed, Errored", 5 * 60, False),
        ("Completed, Cancelled", 5 * 60, False),
    ])
    def test_completed_search_is_reused_only_while_recent_and_complete(self, slskd, method, state, age, reused):
        client, fake = slskd
        old = fake.add_history("Hyperphantasia", state=state, age=age,
                               responses=[peer("an_amy_burger", "Music\\Hyperphantasia\\01.flac")])
        fake.plan_search("Hyperphantasia", [peer("araiguma", f"{HYPERPHANTASIA}\\01 Lone - Track 1.flac")])

        result = run(client, method, "Hyperphantasia")

        assert posts(fake) == ([] if reused else ["Hyperphantasia"])
        assert result["id"] == old if reused else result["id"] != old
        assert [response["username"] for response in result["responses"]] == (
            ["an_amy_burger"] if reused else ["araiguma"]
        )

    @pytest.mark.parametrize("method", ["search", "batch_search"])
    @pytest.mark.parametrize("state, age, reused", [
        ("Completed, TimedOut", 100, True),
        ("Completed, TimedOut", 14 * 60, True),
        ("Completed, TimedOut", 16 * 60, False),
        ("Completed, Errored", 100, False),
        ("Completed, Cancelled", 100, False),
    ])
    def test_search_that_found_nothing_is_reused_for_a_short_time(self, slskd, method, state, age, reused):
        client, fake = slskd
        old = fake.add_history("Shared Night Signals", state=state, age=age)
        fake.plan_search("Shared Night Signals", [peer("late", "Music\\VA\\Shared Night Signals\\01.flac")])

        result = run(client, method, "Shared Night Signals")

        assert posts(fake) == ([] if reused else ["Shared Night Signals"])
        assert (result["id"] == old) is reused
        assert [response["username"] for response in result["responses"]] == ([] if reused else ["late"])

    @pytest.mark.parametrize("method", ["search", "batch_search"])
    def test_search_that_found_files_is_preferred_to_a_newer_empty_one(self, slskd, method):
        client, fake = slskd
        found = fake.add_history("Hyperphantasia", age=30 * 60, responses=[peer("old", "Music\\Hyperphantasia\\01.flac")])
        fake.add_history("Hyperphantasia", age=100)

        result = run(client, method, "Hyperphantasia")

        assert posts(fake) == []
        assert result["id"] == found and [response["username"] for response in result["responses"]] == ["old"]

    @pytest.mark.parametrize("method", ["search", "batch_search"])
    @pytest.mark.parametrize("found_age", [30 * 60, 60])
    def test_unreadable_search_with_files_is_repeated_rather_than_answered_by_an_empty_one(
            self, slskd, method, found_age):
        client, fake = slskd
        # slskd counted files for this query but no longer returns them.
        fake.add_history("Hyperphantasia", age=found_age, response_count=1, file_count=3)
        fake.add_history("Hyperphantasia", age=100)
        fake.plan_search("Hyperphantasia", [peer("araiguma", f"{HYPERPHANTASIA}\\01 Lone - Track 1.flac")])

        result = run(client, method, "Hyperphantasia")

        assert posts(fake) == ["Hyperphantasia"]
        assert [response["username"] for response in result["responses"]] == ["araiguma"]

    @pytest.mark.parametrize("method", ["search", "batch_search"])
    def test_running_search_is_attached_rather_than_reusing_an_empty_one(self, slskd, method):
        client, fake = slskd
        fake.add_history("Lone Temples", age=100)
        fake.add_running("Lone Temples", age=10, responses=[peer("peer", "Music\\Lone\\Temples\\01 Temples.flac")],
                         arrivals=[12.0])

        result = run(client, method, "Lone Temples")

        assert posts(fake) == []
        assert len(result["responses"]) == 1

    @pytest.mark.parametrize("method", ["search", "batch_search"])
    def test_empty_search_reuse_can_be_turned_off(self, slskd, method, monkeypatch):
        client, fake = slskd
        monkeypatch.setattr("discogseek.clients.slskd.SEARCH_EMPTY_REUSE_MAX_AGE", 0.0)
        fake.add_history("Shared Night Signals", age=100)

        run(client, method, "Shared Night Signals")

        assert posts(fake) == ["Shared Night Signals"]

    @pytest.mark.parametrize("method", ["search", "batch_search"])
    @pytest.mark.parametrize("age, reused", [(4 * 60, True), (6 * 60, False)])
    def test_empty_search_reuse_never_outlasts_the_general_reuse_window(self, slskd, method, monkeypatch,
                                                                        age, reused):
        client, fake = slskd
        monkeypatch.setattr("discogseek.clients.slskd.SEARCH_REUSE_MAX_AGE", 5 * 60.0)
        fake.add_history("Shared Night Signals", age=age)

        run(client, method, "Shared Night Signals")

        assert posts(fake) == ([] if reused else ["Shared Night Signals"])

    @pytest.mark.parametrize("method", ["search", "batch_search"])
    def test_use_existing_false_always_searches_again(self, slskd, method):
        client, fake = slskd
        fake.add_history("Hyperphantasia", age=60, responses=[peer("old", "Music\\Hyperphantasia\\01.flac")])
        fake.plan_search("Hyperphantasia", [peer("new", "Music\\Hyperphantasia\\01.flac")])

        result = run(client, method, "Hyperphantasia", use_existing=False)

        assert posts(fake) == ["Hyperphantasia"]
        assert [response["username"] for response in result["responses"]] == ["new"]
        assert ("GET", "/api/v0/searches", None) not in fake.requests


def test_slskd_timestamps_parse_to_utc_epoch_seconds():
    from datetime import datetime, timezone

    from discogseek.clients.slskd import _parse_timestamp

    moment = datetime(2026, 10, 21, 15, 32, 5, tzinfo=timezone.utc).timestamp()
    assert _parse_timestamp("2026-10-21T15:32:05.0639642Z") == pytest.approx(moment + 0.0639642)
    assert _parse_timestamp("2026-10-21T17:32:05+02:00") == moment
    assert _parse_timestamp("2026-10-21T15:32:05") == moment
    assert _parse_timestamp(None) is None
    assert _parse_timestamp("not a date") is None


def test_search_settings_can_be_overridden_from_the_environment(monkeypatch):
    from discogseek.clients.slskd import _env_number

    monkeypatch.setenv("DISCOGSEEK_SEARCH_RESPONSE_LIMIT", "1000")
    assert _env_number("DISCOGSEEK_SEARCH_RESPONSE_LIMIT", 500) == 1000
    for invalid in ("", "lots", "0", "-5"):
        monkeypatch.setenv("DISCOGSEEK_SEARCH_RESPONSE_LIMIT", invalid)
        assert _env_number("DISCOGSEEK_SEARCH_RESPONSE_LIMIT", 500) == 500
    monkeypatch.setenv("DISCOGSEEK_SEARCH_REUSE_MINUTES", "0")
    assert _env_number("DISCOGSEEK_SEARCH_REUSE_MINUTES", 60, minimum=0.0) == 0
    monkeypatch.setenv("DISCOGSEEK_EMPTY_SEARCH_REUSE_MINUTES", "0")
    assert _env_number("DISCOGSEEK_EMPTY_SEARCH_REUSE_MINUTES", 15, minimum=0.0) == 0


@pytest.mark.parametrize("method", ["search", "batch_search"])
def test_dashes_at_word_edges_are_not_sent_as_exclusions(slskd, method):
    # Soulseek reads '-十八時の音楽浴-' as 'exclude 十八時の音楽浴': the search found nothing.
    client, fake = slskd
    folder = "music\\_Compilations\\2016-09 - SHIFT THE CORE ANOTHER -十八時の音楽浴- [STCD-0010]"
    fake.plan_search("SHIFT THE CORE ANOTHER 十八時の音楽浴", [peer("sprite9421", f"{folder}\\08 kyou1110.flac")])

    result = run(client, method, "SHIFT THE CORE ANOTHER -十八時の音楽浴-", timeout=30)

    assert posts(fake) == ["SHIFT THE CORE ANOTHER 十八時の音楽浴"]
    assert len(result["responses"]) == 1


def test_inner_dashes_stay_in_the_search_text(slskd):
    client, fake = slskd

    client.search("Jay-Z Tek-no-Logik - Re:-vive", timeout=30)

    assert posts(fake) == ["Jay-Z Tek-no-Logik - Re:-vive"]


@pytest.mark.parametrize("method", ["search", "batch_search"])
def test_an_earlier_search_with_an_exclusion_is_not_reused(slskd, method):
    # Stored exactly as sent, its exclusion found nothing; without the dash it reads the same.
    client, fake = slskd
    folder = "music\\_Compilations\\2016-09 - SHIFT THE CORE ANOTHER -十八時の音楽浴- [STCD-0010]"
    fake.add_history("SHIFT THE CORE ANOTHER -十八時の音楽浴-", age=60.0)
    fake.plan_search("SHIFT THE CORE ANOTHER 十八時の音楽浴", [peer("sprite9421", f"{folder}\\08 kyou1110.flac")])

    result = run(client, method, "SHIFT THE CORE ANOTHER -十八時の音楽浴-", timeout=30)

    assert posts(fake) == ["SHIFT THE CORE ANOTHER 十八時の音楽浴"]
    assert len(result["responses"]) == 1


def test_queries_that_differ_only_in_case_or_edge_dashes_are_searched_once(slskd):
    client, fake = slskd
    fake.plan_search("One Invitation Red Side", [peer("metabalvin", "MUSIC\\One Invitation -Red Side-\\13. Kyou - Goddamnation.mp3")])

    results = client.batch_search(["One Invitation -Red Side-", "one invitation Red Side"], timeout=30)

    assert posts(fake) == ["One Invitation Red Side"]
    assert len(results["One Invitation -Red Side-"]["responses"]) == 1
    assert len(results["one invitation Red Side"]["responses"]) == 1
