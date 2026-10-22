"""Which peer copies may supply a row: radio captures, DJ mixes and playlist dumps, another
release's generic title ('Intro'), and a title the caller marks as shared by another song."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from discogseek.services.library_download import _mixed_source_folder, download_missing_tracks


def iso(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def transfer(user, filename, state, transfer_id="old-transfer", **fields):
    return {"username": user, "directories": [{"files": [
        {"id": transfer_id, "filename": filename, "state": state, **fields},
    ]}]}


def response(user, *files, **meta):
    return {"username": user, "files": [
        dict(file) if isinstance(file, dict) else {"filename": file, "size": 1000} for file in files
    ], **meta}


def client_with(responses=(), downloads=()):
    """Every query returns `responses`; queries are recorded in order."""
    client = MagicMock()
    client.get_downloads.return_value = list(downloads)
    client.browse_directory.return_value = []
    client.queries = []

    def answer(query):
        client.queries.append(query)
        return {"responses": list(responses)}

    client.search.side_effect = lambda query, **kwargs: answer(query)
    client.batch_search.side_effect = lambda queries, **kwargs: {query: answer(query) for query in queries}
    return client


def picks(result):
    return [(file["user"], file["filename"]) for file in result["queued_files"]]


# Radio captures, DJ-mix segments and playlist dumps (seen in a real artist run).
SHIGETO = {"title": "Friends And Lovers", "artist": "Shigeto", "track_number": 1, "duration": 270.0}
MUX_MOOL = {"title": "Sympathy", "artist": "Mux Mool", "track_number": 5, "duration": 241.0}


@pytest.mark.parametrize("track, release, file", [
    (SHIGETO, "Cambio", {"filename": "slsk\\uploads\\Def Con Radio  SomaFM special mix for Def Con [SomaFM]\\"
                                     "Shigeto - Friends And Lovers.mp3", "size": 1000, "length": 257}),
    (SHIGETO, "Cambio", "@@zsttx\\music\\Napster\\_Ambient & Chill-Out\\DEF CON Radio  SomaFM's year-round channel "
                        "for DEF CON [SomaFM]\\Shigeto - Friends And Lovers.mp3"),
    (MUX_MOOL, "Skulltaste", "@@apoph\\Discomania\\zLONDON BURNING WEB RADIO\\Playlist Auto DJ - 2012 - 48 11122012 "
                             "- in 192\\35. Mux Mool - Sympathy.mp3"),
])
@pytest.mark.parametrize("scope", ["track", "release"])
def test_radio_capture_or_auto_dj_dump_does_not_supply_a_track(track, release, file, scope):
    artist = "Various Artists" if release == "Cambio" else track["artist"]
    client = client_with([response("radio", file)])

    result = download_missing_tracks(client, MagicMock(), artist, release, [dict(track)], search_scope=scope,
                                     dry_run=True)

    assert result["matched_count"] == 0
    assert result["queued_files"] == []


@pytest.mark.parametrize("scope", ["track", "release"])
def test_mix_folder_copy_is_refused_even_at_the_recordings_length(scope):
    capture = {"filename": r"Mixes\Shigeto Boiler Room Detroit\03 Shigeto - Friends And Lovers.flac",
               "size": 1000, "length": 271}
    album = {"filename": r"Music\Shigeto\Lineage\04 Shigeto - Friends And Lovers.mp3", "size": 1000, "bitRate": 192}
    track = dict(SHIGETO)

    result = download_missing_tracks(client_with([response("mixes", capture, hasFreeUploadSlot=True),
                                                  response("album", album)]),
                                     MagicMock(), "Various Artists", "Cambio", [track], search_scope=scope,
                                     dry_run=True)
    assert picks(result) == [("album", album["filename"])]

    # Nothing else is shared: a capture is still not the recording, whatever its length.
    result = download_missing_tracks(client_with([response("mixes", capture)]), MagicMock(),
                                     "Various Artists", "Cambio", [track], search_scope=scope, dry_run=True)
    assert result["matched_count"] == 0 and picks(result) == []


@pytest.mark.parametrize("scope", ["track", "release"])
def test_the_releases_own_folder_supplies_its_tracks_even_when_named_as_a_mix(scope):
    """'Balance 020' is a mixed compilation: its own folder says so and is still its folder."""
    folder = r"@@dnaip\-[Labels]-\Balance Music\[BAL004CD] VA - Balance 020 (Mixed By Deetron) (2011)"
    filename = folder + r"\07 - Lone - Airglow Fires.flac"
    track = {"title": "Airglow Fires", "artist": "Lone", "track_number": 7}

    result = download_missing_tracks(client_with([response("peer", filename)]), MagicMock(), "Various Artists",
                                     "Balance 020", [track], search_scope=scope, dry_run=True)

    assert picks(result) == [("peer", filename)]


def test_live_transfer_from_a_radio_capture_does_not_stand_for_the_row():
    """A transfer an older version queued from a radio rip is not the album recording."""
    radio = (r"radio rips\DEF CON Radio  SomaFM's year-round channel for DEF CON [SomaFM]"
             r"\Shigeto - Friends And Lovers.mp3")
    album = r"Music\Shigeto\Lineage\04 Shigeto - Friends And Lovers.flac"
    client = client_with([response("album", album)],
                         downloads=[transfer("radio", radio, "Queued, Remotely", enqueuedAt=iso(minutes=5))])

    result = download_missing_tracks(client, MagicMock(), "Various Artists", "Cambio", [dict(SHIGETO)],
                                     search_scope="track", dry_run=True)

    assert picks(result) == [("album", album)]


@pytest.mark.parametrize("release, folder", [
    # The requested release is itself a mix: its tracks come from mix folders.
    ("DJ-Kicks: Kamaal Williams", r"Music\Kamaal Williams\2019 - DJ Mix for !K7 (DJ Mix)"),
    ("Late Night Mix Vol. 2", r"Mixes\Late Night (Mixed by Someone)"),
])
def test_a_mix_release_takes_tracks_from_mix_folders(release, folder):
    track = {"title": "Airglow Fires", "artist": "Lone", "track_number": 18}
    filename = folder + r"\18 - Lone - Airglow Fires.flac"

    result = download_missing_tracks(client_with([response("peer", filename)]), MagicMock(), "Various Artists",
                                     release, [track], search_scope="track", dry_run=True)

    assert picks(result) == [("peer", filename)]


@pytest.mark.parametrize("artist, release, track, filename", [
    # An artist named 'Radio', a title 'Radio', and DJ names with 'Mix' are not mixes.
    ("Various Artists", "Modeselektion Vol. 04", {"title": "Introduction", "artist": "Radio Slave", "track_number": 9},
     r"Music\Radio Slave\Radio Slave - Repeat Myself (Remixes)\Radio Slave - Introduction.flac"),
    ("Various Artists", "Chillout Sessions", {"title": "Ambient Brain", "artist": "Mixmaster Morris", "track_number": 2},
     r"Music\Mixmaster Morris\Mixmaster Morris - Hypnotic Highlights\04 Mixmaster Morris - Ambient Brain.flac"),
    ("Artist", "Album", {"title": "Radio", "track_number": 5}, r"Music\Artist\Artist - Other Album\05 Radio.flac"),
    ("Lone", "Reality Testing", {"title": "Airglow Fires", "artist": "Lone", "track_number": 6},
     r"Music\Lone\Lone - Radio Edits & B-Sides\Lone - Airglow Fires.flac"),
])
def test_names_with_radio_or_mix_are_not_mix_folders(artist, release, track, filename):
    result = download_missing_tracks(client_with([response("peer", filename)]), MagicMock(), artist, release,
                                     [track], search_scope="track", dry_run=True)

    assert picks(result) == [("peer", filename)]


@pytest.mark.parametrize("folder", [
    r"radio rips\#Dubstep - 24-7 Dubstep Radio - 24Dubstep.net",
    r"@@dgisc\complete\maf9000\Xmas in Frisko  SomaFM's eclectic holiday mix. Not for the easily offended.",
    r"@@mlsgq\Music\Jonathan Richman\Jonathan Richman Penthouse Radio Show 11-20-1983",
    r"@@rknrd\podcasts\torrents\2021\Grill Stream",
    r"@@gpulv\Radios\Anel Radio\# House Old",
    r"Music\_VA\DJ-KiCKS\2019-11-08 - DJ-KiCKS - Kamaal Williams (Mixed)",
    r"@@vhjrg\Треки\Новое\DRUM\VA-Rollercoaster_(Mixed_By_Drum_Origins)-TRIPLECD003-2002-sour",
    r"MoS Annuals\1998 - The Annual IV Mixed By Judge Jules & Boy George (2-CD) [ANNCD98]\CD2",
    r"@@fmycf\Music\We Are All Astronauts\DJ Mixes and Remixes\2014 - Blue Dot I (DJ Mix)",
    r"shared\Artists\M\Machinedrum\[2014-11-17] Vapor City Archives Mini-Mix",
    r"@@kuukf\Soulseek (2021)\Kerri Chandler Boiler Room",
    r"music\Kelela - Aquaphoria (Mixtape)_2019_MP3 320",
    r"Music\Compilations\Playlist Collections\Playlist - Beach Remixes - 226 Tracks",
    r"@@apgin\iTunes Music\múm\Live at Great American Music Hall",
    r"@@pbkts\MUSIC-FLAC\David Bowie\2000 - Bowie At The Beeb - The Best Of The  BBC Radio Sessions 68-72",
])
def test_mix_and_radio_folders_are_recognised(folder):
    assert _mixed_source_folder(folder.replace("\\", "/"))


@pytest.mark.parametrize("folder", [
    r"@@jlksj\new_music\Radio Slave - Repeat Myself (Remixes) [ WORK THEM]",
    r"@@fmycf\Music\All India Radio\2014 - Fall Remixes + Original Album",
    r"@@oemyp\@\VA - Harmless Records\CTYCD96 - Radio One Sound City Leeds - 1996",
    r"@@cmgjq\jkmk.net\06. Jun\18\Gorgon City - Work It Out (Extended Mix) - 2026-06-19",
    r"@@eewpi\Music\Brooklyn Bounce\Albums\1998 - Re-Mixed Collection (Club Tools #006598-2 CLU)",
    r"@@cpmyj\@TOR\D&B_MP3_Genre_Pack-(G-H-I)-ViNYL+WEB-MiXED\High_Contrast--Remixes_and_Rarities-WEB-2020-OMA",
    r"@@ffzmb\SOULSEEK\UNSORTED\complete\Marie's Playlist\14.9.13\complete\VA-Philly_Regrooved_3-2CD-2013-DLiTE",
    r"@@gpulv\DJ Set\# Compilations\4 DVDs full of music\MP3Vol3",
    r"Mixes + Compilations\Mixmaster Morris - Hypnotic Highlights",
    r"@@gcqcv\music\++electronica++\va - Sci.Fi.Hi.Fi\Sci-Fi Hi-Fi- 04 - Funk D'Void [SOMA CD064 {UK} CD]",
    r"Music\Danny Daze - BLUE (Remixes)",
    r"NTO-EM4\EMLPS\Merck Records NT\[MERCK MIX1] Merck Mix 1 - Spring 2003 (2003)",
    # The unmixed edition of a mix compilation holds the full tracks (seen in the corpus).
    r"@@vohmw\Rips\2011\Deetron-Balance 020 (Mixed By Deetron) [Un-Mixed Version] Un-Mixed Version-(Balance Music)"
    r"-16BIT-WEB-FLAC-2011-SLSK",
    r"Music\VA\Anjunadeep 09 (Unmixed)\CD1",
    # Legacy's 'Playlist: The Very Best Of ...' is an artist's own best-of, not a playlist dump.
    r"Music\Journey\Playlist_ The Very Best of Journey (2008)",
    r"Music\Toto\Toto - Playlist - The Very Best Of Toto",
    # Ensembles named after a broadcaster are performers, not radio captures.
    r"Music\BBC Radio Orchestra\BBC Radio Orchestra - Strings In Hi-Fi (1975)",
    r"Jazz\BBC Radio Big Band\1990 - Sounds Of The Swing Era",
])
def test_artists_venues_and_remix_releases_are_not_mix_folders(folder):
    assert not _mixed_source_folder(folder.replace("\\", "/"))


@pytest.mark.parametrize("scope", ["track", "release"])
def test_unmixed_edition_of_a_mix_compilation_supplies_the_full_track(scope):
    filename = (r"@@vohmw\Rips\2011\Deetron-Balance 020 (Mixed By Deetron) [Un-Mixed Version] Un-Mixed Version-"
                r"(Balance Music)-16BIT-WEB-FLAC-2011-SLSK\2-07 - Lone - Airglow Fires.flac")
    file = {"filename": filename, "size": 1000, "length": 301}
    track = {"title": "Airglow Fires", "artist": "Lone", "track_number": 6, "duration": 300.0}

    result = download_missing_tracks(client_with([response("peer", file)]), MagicMock(), "Lone", "Reality Testing",
                                     [track], search_scope=scope, dry_run=True)

    assert picks(result) == [("peer", filename)]


# Another release's 'Intro' by the same artist is another recording.
INTRO = {"title": "Intro", "artist": "Lone", "track_number": 1, "duration": 95.0}
FIRST_LIGHT_INTRO = r"Music\Lone\First Light\01 Intro.flac"
OWN_INTRO = r"Share\Lone\Phonetic Dreams\01 Intro.flac"


def test_live_transfer_of_another_releases_intro_does_not_fulfil_the_row():
    client = client_with([response("own", OWN_INTRO)],
                         downloads=[transfer("artist-run", FIRST_LIGHT_INTRO, "Queued, Remotely", enqueuedAt=iso(minutes=5))])

    result = download_missing_tracks(client, MagicMock(), "Lone", "Phonetic Dreams", [dict(INTRO)],
                                     search_scope="track")

    assert client.queries
    assert picks(result) == [("own", OWN_INTRO)]
    assert client.enqueue_download.call_args.args[0] == "own"


def test_generic_title_outside_the_release_needs_a_matching_length():
    elsewhere = {"filename": r"Music\Lone\Galaxy Garden\01 Lone - Intro.flac", "size": 1000}

    result = download_missing_tracks(client_with([response("peer", elsewhere)]), MagicMock(), "Lone",
                                     "Phonetic Dreams", [dict(INTRO)], search_scope="track", dry_run=True)
    assert result["matched_count"] == 0

    timed = dict(elsewhere, length=96)
    result = download_missing_tracks(client_with([response("peer", timed)]), MagicMock(), "Lone",
                                     "Phonetic Dreams", [dict(INTRO)], search_scope="track", dry_run=True)
    assert picks(result) == [("peer", timed["filename"])]


def test_exclusive_title_comes_only_from_the_release_folder():
    album = r"music\Machinedrum\Urban Biology\05 Machinedrum - Floss.flac"
    single = r"music\Machinedrum\Floss (Single)\01 Machinedrum - Floss.flac"
    track = {"title": "Floss", "artist": "Machinedrum", "track_number": 1}

    def run(row, *files):
        client = client_with([response("peer", *files)])
        return download_missing_tracks(client, MagicMock(), "Machinedrum", "Floss", [row], search_scope="track",
                                       dry_run=True)

    assert picks(run(dict(track), album)) == [("peer", album)]
    assert run({**track, "exclusive_title": True}, album)["matched_count"] == 0
    assert picks(run({**track, "exclusive_title": True}, album, single)) == [("peer", single)]
