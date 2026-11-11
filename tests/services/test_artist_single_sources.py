"""Songs peers share only as another release's copy, or name after a one-song release.

Seen in Rotator's discography: digital singles re-releasing compilation tracks ('HXC Militia'
on 'Disaster Must Come', 'No Surrenda!' on 'Insized'), a compilation track found only on
another compilation, and a soundtrack single shared as 'ROTATOR - Orgasmic Nightmare - PoFF
Night After Movie Soundtrack.mp3' for its only track 'Orgasmic Nightmare'.
"""

import pytest

from tests.helpers.artist_mode import (
    ARTIST,
    ARTIST_ID,
    FakeArtistClient,
    local_track,
    raw_catalog,
    release,
    response,
    run_scraper,
    scraper_for,
)


def compilation(release_id, title, tracks):
    """A Various Artists release of (title, artist name, artist id, recording id) tracks."""
    def credit(name, artist_id):
        return [{"artist": {"id": artist_id, "name": name}, "name": name}]

    return {
        "id": release_id, "title": title, "artist-credit": credit("Various Artists", "va"),
        "release-group": {"id": f"{release_id}-group", "primary-type": "Album", "secondary-type-list": ["Compilation"]},
        "medium-list": [{"position": "1", "track-list": [
            {"id": f"{release_id}-t{number}", "number": str(number), "position": str(number), "title": name,
             "artist-credit": credit(artist, artist_id), "recording": {"id": recording, "title": name}}
            for number, (name, artist, artist_id, recording) in enumerate(tracks, 1)
        ]}],
    }


ALBUM = "Disaster Must Come"
ALBUM_COPY = r"@@b\Music\Target Artist - Disaster Must Come\12 HXC Militia.flac"


def single_catalog():
    # The album and the single have different recordings: different songs to MusicBrainz.
    return raw_catalog([
        release(ALBUM, ["Come Get Some", "HXC Militia"], 0),
        release("HXC Militia", ["HXC Militia"], 1, primary_type="Single"),
    ])


@pytest.mark.parametrize("dry_run", [False, True])
def test_another_releases_copy_is_reported_but_never_queued_for_a_single(monkeypatch, dry_run):
    raw = single_catalog()
    client = FakeArtistClient(responder=lambda query: [response("bpeer", ALBUM_COPY)] if "Militia" in query else [])
    library = [local_track(raw, 0, 1), local_track(raw, 0, 2)]

    result = run_scraper(scraper_for(raw, client, dry_run=dry_run), monkeypatch, raw, library=library)

    # The album's copy is that album's song: queueing it would download it a second time.
    assert client.enqueued_names() == []
    assert result["enqueued_count"] == 0
    assert [(entry["release"], entry["title"], entry["reason"], entry.get("folder"))
            for entry in result["unresolved_tracks"]] == [
        ("HXC Militia", "HXC Militia", "only another release's copy found",
         r"@@b\Music\Target Artist - Disaster Must Come"),
    ]


def test_a_singles_own_copy_is_queued_when_another_releases_copy_is_also_shared(monkeypatch):
    raw = single_catalog()
    own = r"@@c\Music\Target Artist\Singles\Target Artist - HXC Militia.flac"
    client = FakeArtistClient(responder=lambda query: [
        response("bpeer", ALBUM_COPY), response("cpeer", own),
    ] if "Militia" in query else [])
    library = [local_track(raw, 0, 1), local_track(raw, 0, 2)]

    result = run_scraper(scraper_for(raw, client), monkeypatch, raw, library=library)

    assert client.enqueued_names() == [own]
    assert result["unresolved_tracks"] == []


def test_one_song_release_shared_under_the_release_title(monkeypatch):
    title = "Orgasmic Nightmare - PoFF Night After Movie Soundtrack"
    raw = raw_catalog([release(title, ["Orgasmic Nightmare"], 0, primary_type="Single")])
    copy = (r"@@q\!LABELS\!peace off\[none] Target Artist - Orgasmic Nightmare - PoFF Night After Movie "
            r"Soundtrack (2013)\[unnamed].  - TARGET ARTIST - Orgasmic Nightmare - PoFF Night After Movie "
            r"Soundtrack.mp3")
    client = FakeArtistClient(responder=lambda query: [response("deeppeas", copy)] if "Orgasmic" in query else [])

    result = run_scraper(scraper_for(raw, client), monkeypatch, raw)

    assert client.enqueued_names() == [copy]
    assert result["unresolved_tracks"] == []


def test_a_release_title_is_no_alternative_title_on_a_release_with_several_songs(monkeypatch):
    # 'Album - Subtitle.mp3' on a two-track release may be either track, or the whole release.
    title = "Orgasmic Nightmare - PoFF Night After Movie Soundtrack"
    raw = raw_catalog([release(title, ["Orgasmic Nightmare", "Night After"], 0, primary_type="EP")])
    copy = rf"@@q\Music\Target Artist\Target Artist - {title}.mp3"
    client = FakeArtistClient(responder=lambda query: [response("deeppeas", copy)])

    result = run_scraper(scraper_for(raw, client, dry_run=True), monkeypatch, raw)

    assert result["matched_files"] == []
    assert sorted(entry["title"] for entry in result["unresolved_tracks"]) == ["Night After", "Orgasmic Nightmare"]


def test_compilation_track_found_only_on_another_compilation_is_reported_not_taken(monkeypatch):
    destruktion = "Help Me to Keep Up Destruktion!!!"
    brick = compilation("brick", "Brick Smack", [
        ("Opening", "Other Artist", "other", "brick-1"), (destruktion, ARTIST, ARTIST_ID, "brick-rec"),
    ])
    sic = compilation("sic", "(sic): The Broklyn Beats 7\"s", [
        ("Chicken Boogie XXX", "Third Artist", "third", "sic-1"), (destruktion, ARTIST, ARTIST_ID, "sic-rec"),
    ])
    raw = raw_catalog([])
    raw["releases_track_artist"] = [brick, sic]
    copy = r"@@d\!LABELS\!Drosstik\[DTK01] VA-Brick_Smack-(Drosstik)-2CD-2003\02. Target Artist - Help Me To Keep Up Destruktion!!!.mp3"
    client = FakeArtistClient(responder=lambda query: [response("deeppeas", copy)])

    result = run_scraper(scraper_for(raw, client), monkeypatch, raw)

    # Brick Smack's own folder supplies Brick Smack's track, once.
    assert client.enqueued_names() == [copy]
    assert [(entry["release"], entry["title"], entry["reason"]) for entry in result["unresolved_tracks"]] == [
        ("(sic): The Broklyn Beats 7\"s", destruktion, "only another release's copy found"),
    ]


def test_a_file_named_after_another_release_is_that_releases_copy(monkeypatch):
    # The folder names no release, but the file names the compilation the song comes from.
    raw = raw_catalog([release("No Surrenda!", ["No Surrenda!"], 0, primary_type="Single")])
    raw["releases_track_artist"] = [compilation("insized", "Insized", [
        ("Opening", "Other Artist", "other", "ins-1"), ("No Surrenda!!!", ARTIST, ARTIST_ID, "ins-rec"),
    ])]
    copy = (r"ln\personal\laxes\$Others\TARGET ARTIST- STAZMA-\TARGET ARTIST, STAZMA - -RANC006- VA - Insized -2CD-"
            r" - 02 Target Artist - No Surrenda !!!.mp3")
    client = FakeArtistClient(responder=lambda query: [response("cloverpiral", copy)])

    result = run_scraper(scraper_for(raw, client), monkeypatch, raw)

    assert client.enqueued_names() == [copy]  # for Insized, not again for the single
    assert [(entry["release"], entry["reason"]) for entry in result["unresolved_tracks"]] == [
        ("No Surrenda!", "only another release's copy found"),
    ]


def test_a_file_named_only_after_its_song_is_no_other_releases_copy(monkeypatch):
    # 'HXC Militia' is a single and a compilation track: a loose 'Target Artist - HXC Militia'
    # names the song, not the single.
    raw = raw_catalog([release("HXC Militia", ["HXC Militia"], 0, primary_type="Single")])
    raw["releases_track_artist"] = [compilation("chaos", "You Want Some Chaos", [
        ("Opening", "Other Artist", "other", "chaos-1"), ("HXC Militia", ARTIST, ARTIST_ID, "chaos-rec"),
    ])]
    library = [local_track(raw, 0, 1)]
    loose = r"@@m\Music\Target Artist\Target Artist - HXC Militia.mp3"
    client = FakeArtistClient(responder=lambda query: [response("loosepeer", loose)])

    result = run_scraper(scraper_for(raw, client), monkeypatch, raw, library=library)

    assert client.enqueued_names() == [loose]
    assert result["unresolved_tracks"] == []


@pytest.mark.parametrize("length, queued", [(371, False), (477, True)])
def test_a_loose_copy_of_a_clearly_different_length_is_another_edit(monkeypatch, length, queued):
    # 'HOMICIDE 11' holds the 478 s 'JiHAD'; a compilation's 'Jihad' runs 371 s.
    single = release("HOMICIDE 11", ["JiHAD"], 0, primary_type="Single")
    single["medium-list"][0]["track-list"][0]["length"] = 478238
    raw = raw_catalog([single])
    copy = r"@@g\MUZIK\COMPILATIONS\Un Pave Dans l'Asphalte (2013)\61 - Target Artist - Jihad.mp3"
    client = FakeArtistClient(responder=lambda query: [{
        "username": "Bonhommet25", "uploadSpeed": 5000, "queueLength": 0, "hasFreeUploadSlot": True,
        "files": [{"filename": copy, "size": 9_000_000, "length": length}],
    }])

    result = run_scraper(scraper_for(raw, client), monkeypatch, raw)

    assert client.enqueued_names() == ([copy] if queued else [])
    assert [entry["title"] for entry in result["unresolved_tracks"]] == ([] if queued else ["JiHAD"])


def test_a_one_song_release_title_without_the_songs_version_is_no_alternative_title(monkeypatch):
    # The single 'Song A' holds only 'Song A (Instrumental)': 'Target Artist - Song A.mp3' is the vocal song.
    raw = raw_catalog([
        release("Album", ["Song A", "Other Song"], 0),
        release("Song A", ["Song A (Instrumental)"], 1, primary_type="Single"),
    ])
    library = [local_track(raw, 0, 1), local_track(raw, 0, 2)]
    loose = r"@@p\Music\Target Artist\Target Artist - Song A.mp3"
    client = FakeArtistClient(responder=lambda query: [response("loosepeer", loose)])

    result = run_scraper(scraper_for(raw, client), monkeypatch, raw, library=library)

    assert client.enqueued_names() == []
    assert [entry["title"] for entry in result["unresolved_tracks"]] == ["Song A (Instrumental)"]


def test_a_file_naming_its_own_release_is_not_a_shorter_titled_releases_copy(monkeypatch):
    # 'Mosaic World Remix Series Vol.10' contains the title of the compilation 'Mosaic World'.
    raw = raw_catalog([release("Mosaic World Remix Series Vol.10", ["Night Drive (Totsumal Remix)"], 0, primary_type="EP")])
    raw["releases_track_artist"] = [compilation("mw", "Mosaic World", [
        ("Opening", "Other Artist", "other", "mw-1"), ("Night Drive", ARTIST, ARTIST_ID, "mw-rec"),
    ])]
    copy = r"@@m\Music\Target Artist\Target Artist - Mosaic World Remix Series Vol.10 - 01 - Night Drive (Totsumal Remix).mp3"
    client = FakeArtistClient(responder=lambda query: [response("mpeer", copy)] if "Totsumal" in query else [])

    result = run_scraper(scraper_for(raw, client, dry_run=True), monkeypatch, raw)

    assert not any(entry["reason"] == "only another release's copy found" for entry in result["unresolved_tracks"])
    assert any(file["filename"] == copy for download in result["release_downloads"] for file in download["queued_files"])


@pytest.mark.parametrize("length, queued", [(200, True), (260, False)])
def test_a_generic_title_in_an_unknown_folder_counts_at_the_songs_length(monkeypatch, length, queued):
    # '[2012] Soutou no Rei' is the album under a romanized name the catalog does not know.
    album = release("双頭の零", ["Kizuna Tsunagu", "Rain", "Hikari no Michi"], 0)
    album["medium-list"][0]["track-list"][1]["length"] = 200000
    raw = raw_catalog([album])
    library = [local_track(raw, 0, 1), local_track(raw, 0, 3)]
    copy = r"@@s\Music\Target Artist\[2012] Soutou no Rei\02 - Target Artist - Rain.flac"
    client = FakeArtistClient(responder=lambda query: [{
        "username": "speer", "uploadSpeed": 5000, "queueLength": 0, "hasFreeUploadSlot": True,
        "files": [{"filename": copy, "size": 20_000_000, "length": length}],
    }] if "Rain" in query else [])

    result = run_scraper(scraper_for(raw, client), monkeypatch, raw, library=library)

    assert client.enqueued_names() == ([copy] if queued else [])
    assert [entry["title"] for entry in result["unresolved_tracks"]] == ([] if queued else ["Rain"])


def peer(user, folder, files, queue=0):
    """A search response of (name, length) files in one folder."""
    return {"username": user, "uploadSpeed": 5000, "queueLength": queue, "hasFreeUploadSlot": True,
            "files": [{"filename": f"{folder}\\{name}", "size": 8_000_000, "length": length} for name, length in files]}


def test_a_rip_at_the_wrong_speed_is_no_copy_of_the_release(monkeypatch):
    # The EP's songs run at 368 s and 267 s; a sped-up rip holds them at 262 s and 192 s.
    ep = release("Help Me Keep Up Destruktion", ["Distorted Species", "Meloko Dolomo", "Labba Labba"], 0)
    for track, length in zip(ep["medium-list"][0]["track-list"], (353000, 368000, 267000)):
        track["length"] = length
    seven = release("Help Me To Keep Up Destruction!!! / Chicken Boogie",
                    ["Help Me To Keep Up Destruction!!!", "Chicken Boogie"], 1, primary_type="Single")
    raw = raw_catalog([ep, seven])
    library = [local_track(raw, 0, number) for number in (1, 2, 3)]
    fast = peer("BullshitPancake", r"@@t\Fucked Up EDM\target artist - 2006 - help me to keep up destruction [broklyn beats]", [
        ("02-target_artist-meloko_dolomo.mp3", 262), ("03-target_artist-labba_labba.mp3", 192),
        ("04-target_artist-help_me_to_keep_up_destruction.mp3", 145), ("05-target_artist-chicken_boogie.mp3", 187),
    ])
    proper = peer("koalabeer", r"Music\Breakcore\Target_Artist-Help_Me_To_Keep_Up_Destruction-(BB023)-Proper-Vinyl-2006", [
        ("02-target_artist-meloko_dolomo.mp3", 368), ("03-target_artist-labba_labba.mp3", 267),
        ("04-target_artist-help_me_to_keep_up_destruction.mp3", 203), ("05-target_artist-chicken_boogie.mp3", 269),
    ], queue=1192)
    client = FakeArtistClient(responder=lambda query: [fast, proper])

    run_scraper(scraper_for(raw, client), monkeypatch, raw, library=library)

    assert {user for user, _ in client.calls} == {"koalabeer"}
    assert sorted(name.rsplit("\\", 1)[-1] for name in client.enqueued_names()) == [
        "04-target_artist-help_me_to_keep_up_destruction.mp3", "05-target_artist-chicken_boogie.mp3",
    ]


@pytest.mark.parametrize("length, queued", [(250, False), (334, True)])
def test_another_editions_length_rules_out_a_loose_edit(monkeypatch, length, queued):
    # The fullest edition (numbering) has no lengths; the other edition of the group times 'Enemy' at 333 s.
    vinyl = release("He Who Makes A Beast", ["Breakstarzz", "Bonus Loop", "Enemy"], 0, group="beast",
                    recordings=["rec-breakstarzz", "rec-loop", "rec-enemy"])
    cd = release("He Who Makes A Beast", ["Breakstarzz", "Enemy"], 1, group="beast",
                 recordings=["rec-breakstarzz", "rec-enemy"])
    for track, seconds in zip(cd["medium-list"][0]["track-list"], (413000, 333000)):
        track["length"] = seconds
    raw = raw_catalog([vinyl, cd])
    loose = (r"@@l\Music\Target Artist\Singles", [("Target Artist - Enemy.mp3", length)])
    client = FakeArtistClient(responder=lambda query: [peer("loosepeer", *loose)] if "Enemy" in query or query == ARTIST else [])

    # Nothing is in the library, so the fullest edition (no lengths) numbers the release.
    result = run_scraper(scraper_for(raw, client), monkeypatch, raw)

    assert any(name.endswith("Enemy.mp3") for name in client.enqueued_names()) is queued
    assert ("Enemy" in [entry["title"] for entry in result["unresolved_tracks"]]) is not queued


def test_a_misspelled_folder_of_another_release_is_still_that_releases_copy(monkeypatch):
    raw = single_catalog()
    copy = r"musicorganizedforrealthistime\Target Artist vs Mr KiLL\Disaster must Comes\12 HXC Militia.mp3"
    client = FakeArtistClient(responder=lambda query: [response("geqgiuq", copy)] if "Militia" in query else [])
    library = [local_track(raw, 0, 1), local_track(raw, 0, 2)]

    result = run_scraper(scraper_for(raw, client), monkeypatch, raw, library=library)

    assert client.enqueued_names() == []
    assert [entry["reason"] for entry in result["unresolved_tracks"]] == ["only another release's copy found"]


def test_a_mix_cd_holding_an_eps_titles_does_not_stand_for_the_ep(monkeypatch):
    # 'Fuck Shit Up X-Toxic' (a mix CD) holds the four titles of 'Dissident Sound Maniak' as
    # mix segments; queueing the CD must not make the EP's own copy look already queued.
    cd_titles = ["Untitled", "Pump Diz Party", "Mutanoid Soundbwoy", "Don't Sleep Ghostly", "Fight Back", "Big Booty"]
    ep_titles = ["Mutanoid Soundbwoy", "Don't Sleep Ghostly!!!", "Big Booty", "Fight Back"]
    ep = release("Dissident Sound Maniak Part 1.0", ep_titles, 1, primary_type="EP")
    for track, seconds in zip(ep["medium-list"][0]["track-list"], (284000, 311000, 231000, 324000)):
        track["length"] = seconds
    raw = raw_catalog([release("Fuck Shit Up X-Toxic", cd_titles, 0), ep])
    mix = peer("rederik", r"M\General\Target Artist\POFF CD MIX 02-Target Artist-Fuck Shit Up X-Toxic-2005", [
        (f"{number:02d} - {title}.mp3", seconds)
        for number, (title, seconds) in enumerate(zip(cd_titles, (17, 270, 212, 368, 232, 242)), 1)
    ])
    own = peer("geqgiuq", r"music\Target Artist\Dissident Sound Maniak Part 1.0", [
        (f"{side} {title}.flac", seconds) for side, title, seconds in zip(["A1", "A2", "B1", "B2"], ep_titles, (284, 311, 231, 324))
    ])
    client = FakeArtistClient(responder=lambda query: [mix, own])

    result = run_scraper(scraper_for(raw, client), monkeypatch, raw)

    assert sorted((user, len(files)) for user, files in client.calls) == [("geqgiuq", 4), ("rederik", 6)]
    assert result["unresolved_tracks"] == []


@pytest.mark.parametrize("album,folder", [
    # A digit inside a word spells a letter; it is no catalog number.
    ("ZombiN8or", r"@@b\Music\Target Artist\2014-08 - ZombiN8or [MDSTCD-046]"),
    # Every word of the title is one folders use for anything.
    ("The Singles vol.1", r"@@b\Music\Fujimi Records (Target Artist) - The Singles vol.1 [FIR168] (M3-53)"),
])
def test_copy_in_a_folder_of_a_release_with_an_unusual_title_is_that_releases(monkeypatch, album, folder):
    titles = ["Come Get Some", "Bit Genocide", "Dam Dam", "Tengu"]
    raw = raw_catalog([
        release(album, titles, 0),
        release("Bit Genocide", ["Bit Genocide"], 1, primary_type="Single"),
    ])
    copy = folder + r"\02 - Target Artist - Bit Genocide.flac"
    client = FakeArtistClient(responder=lambda query: [response("bpeer", copy)] if "Genocide" in query else [])
    library = [local_track(raw, 0, number) for number in range(1, 5)]

    result = run_scraper(scraper_for(raw, client), monkeypatch, raw, library=library)

    assert client.enqueued_names() == []
    assert [(entry["release"], entry["reason"], entry.get("folder")) for entry in result["unresolved_tracks"]] == [
        ("Bit Genocide", "only another release's copy found", folder),
    ]


@pytest.mark.parametrize("folder", [
    r"@@b\Music\Target Artist\The Singles vol.10",   # another volume the catalog does not list
    r"@@b\Music\Target Artist\Singles",              # a folder of loose singles
    r"@@b\Music\Target Artist [FIR115] 2019",        # codes and years name no release
])
def test_copy_in_a_folder_that_names_no_catalogued_release_is_taken(monkeypatch, folder):
    titles = ["Come Get Some", "Bit Genocide", "Dam Dam", "Tengu"]
    raw = raw_catalog([
        release("The Singles vol.1", titles, 0),
        release("Bit Genocide", ["Bit Genocide"], 1, primary_type="Single"),
    ])
    copy = folder + r"\Target Artist - Bit Genocide.flac"
    client = FakeArtistClient(responder=lambda query: [response("bpeer", copy)] if "Genocide" in query else [])
    library = [local_track(raw, 0, number) for number in range(1, 5)]

    result = run_scraper(scraper_for(raw, client), monkeypatch, raw, library=library)

    assert client.enqueued_names() == [copy]
    assert result["unresolved_tracks"] == []


ARCHIVE_TITLES = ["Itaconate", "Missing Link", "Front Line", "Hydrogen"]


def archive_catalog():
    """An album titled in Japanese, and a compilation with its own recording of one of its songs."""
    raw = raw_catalog([release("夜の信号アーカイブス", ARCHIVE_TITLES, 0)])
    raw["releases_track_artist"] = [compilation("va-1", "Twenty Years of Noise", [
        ("Itaconate", ARTIST, ARTIST_ID, "va-rec-itaconate"), ("Other Song", "Other Artist", "other", "va-rec-other"),
    ])]
    return raw


def test_folder_holding_another_releases_tracklist_is_that_release_whatever_its_name(monkeypatch):
    raw = archive_catalog()
    # The peer names the album in English: nothing in the folder's name says which release it is.
    folder = r"@@a\Music\(2018.04.29) - Target Artist - Night Signal Archives - (flac)"
    files = [f"{folder}\\1-{number:02d} - Target Artist - {title}.flac" for number, title in enumerate(ARCHIVE_TITLES, 1)]
    client = FakeArtistClient(responder=lambda query: [response("apeer", *files)] if query == ARTIST else [])
    library = [local_track(raw, 0, number) for number in range(1, 5)]

    result = run_scraper(scraper_for(raw, client), monkeypatch, raw, library=library)

    assert client.enqueued_names() == []
    assert [(entry["release"], entry["title"], entry["reason"], entry.get("folder"))
            for entry in result["unresolved_tracks"]] == [
        ("Twenty Years of Noise", "Itaconate", "only another release's copy found", folder),
    ]


def test_folder_with_a_few_songs_of_another_release_is_no_copy_of_it(monkeypatch):
    raw = archive_catalog()
    folder = r"@@a\Music\Target Artist - Favourites"
    files = [f"{folder}\\Target Artist - {title}.flac" for title in ARCHIVE_TITLES[:3]]
    client = FakeArtistClient(responder=lambda query: [response("apeer", *files)] if query == ARTIST else [])
    library = [local_track(raw, 0, number) for number in range(1, 5)]

    result = run_scraper(scraper_for(raw, client), monkeypatch, raw, library=library)

    assert client.enqueued_names() == [files[0]]
    assert result["unresolved_tracks"] == []


def test_same_named_folder_of_a_peer_that_showed_few_files_is_that_release_too(monkeypatch):
    raw = archive_catalog()
    whole = r"@@a\Music\(2018.04.29) - Target Artist - Night Signal Archives - (flac)"
    files = [f"{whole}\\1-{number:02d} - Target Artist - {title}.flac" for number, title in enumerate(ARCHIVE_TITLES, 1)]
    # This peer's client cut its search response: one file of the same folder.
    cut = r"@@b\Doujin\Target Artist\Target Artist - Night Signal Archives\01. Target Artist - Itaconate.flac"
    client = FakeArtistClient(responder=lambda query: (
        [response("apeer", *files), response("bpeer", cut)] if query == ARTIST else []
    ))
    library = [local_track(raw, 0, number) for number in range(1, 5)]

    result = run_scraper(scraper_for(raw, client), monkeypatch, raw, library=library)

    assert client.enqueued_names() == []
    assert [entry["reason"] for entry in result["unresolved_tracks"]] == ["only another release's copy found"]
