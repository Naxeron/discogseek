# discogseek

A command-line tool to audit music against MusicBrainz, browse incomplete releases, and search slskd to queue missing tracks. Libraries can be local files, Navidrome/Subsonic, or both. slskd handles transfers and download destinations.

## Setup

Requires Python 3.8+. Downloads require a running slskd instance.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

Set environment variables or put them in a `.env` file:

```dotenv
DISCOGSEEK_LIBRARY_DIR=/path/to/music
DISCOGSEEK_BROWSER_LIBRARY_DIR=/mnt/music/Library
DISCOGSEEK_CACHE_DIR=/path/to/cache/discogseek
SLSKD_URL=http://localhost:5030
SLSKD_API_KEY=your-api-key

# Alternatively, authenticate slskd with SLSKD_USERNAME and SLSKD_PASSWORD.

# Optional remote library (included in audits and download pre-scans):
NAVIDROME_URL=http://localhost:4533
NAVIDROME_USERNAME=your-username
NAVIDROME_PASSWORD=your-password
```

`SUBSONIC_URL`, `SUBSONIC_USERNAME`, and `SUBSONIC_PASSWORD` are also accepted. Local and remote tracks are reconciled together. A configured remote library must be reachable before downloading.

The cache defaults to `~/.cache/discogseek`. For `audit` and `download`, the local library defaults to `/mnt/music` when present, otherwise `./music`. The browser defaults to `/mnt/music/Library`; set `DISCOGSEEK_BROWSER_LIBRARY_DIR` to change its default independently. Use `-d` to select a library or a release folder. An absent local directory is treated as empty, allowing remote-only audits or new discography downloads.

Optional settings for searches and transfer recovery, described below:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DISCOGSEEK_SEARCH_RESPONSE_LIMIT` | 500 | Peer responses slskd collects per search |
| `DISCOGSEEK_SEARCH_FILE_LIMIT` | 50000 | Files slskd collects per search |
| `DISCOGSEEK_SEARCH_REUSE_MINUTES` | 60 | Reuse an identical search slskd finished this recently; `0` turns reuse off |
| `DISCOGSEEK_EMPTY_SEARCH_REUSE_MINUTES` | 15 | Reuse an identical search that found no files this recently (at most `DISCOGSEEK_SEARCH_REUSE_MINUTES`); `0` turns this off |
| `DISCOGSEEK_STALE_QUEUE_HOURS` | 6 | Treat a transfer still waiting in a queue this long as stale |
| `DISCOGSEEK_PEER_COOLDOWN_HOURS` | 6 | Pause a peer after a temporary refusal |
| `DISCOGSEEK_SLSKD_DOWNLOADS_DIR` | slskd's downloads setting | slskd's downloads folder as seen from this machine |
| `DISCOGSEEK_IMPORT_WINDOW_HOURS` | 24 | Without that folder, how long a finished download counts as awaiting import |

## Audit

```bash
# Find missing tracks across an artist's catalog
discogseek audit "Artist" -d /path/to/music --missing-only

# Audit one release, including entirely absent releases
discogseek audit "Artist" --release "Album"

# Select an exact edition using its MusicBrainz release MBID
discogseek audit "Artist" --release-id 00000000-0000-0000-0000-000000000000

# Machine-readable output, or file exports
discogseek audit "Artist" --json -
discogseek audit "Artist" --json audit.json --csv audit.csv --txt missing.txt
```

Artist queries accept names or MusicBrainz artist MBIDs. For a release query, supply the artist name and either a release title or release MBID. Use `--release-id` when editions or titles are ambiguous. A release that cannot be resolved in MusicBrainz is reported as unverified, not complete.

Release audits (`--release`, `--release-id` and the browser) list the printed track titles and also accept each recording's own title (`alt_titles` in JSON). Files tagged with MusicBrainz recording or track IDs are matched by ID before any title comparison. A file matches at its disc/track position, its position counted across all discs, or its vinyl side position (`A1`, `B2`); without disc tags, a title found only once on the release can match on another disc. Files that match no official track stay listed as extras but do not count as found. A release found by title search must credit your local artist, and one that matches none of your files is unverified. Only releases credited to Various Artists, or compilations mostly by artists other than the credited curator, are treated as Various Artists; single-artist best-ofs and remix albums keep their artist.

`--full-scan` inspects all local tags during an artist audit instead of targeting likely files. `--force-refresh` refreshes MusicBrainz metadata; scans reuse metadata cached for unchanged local files, rereading a file once when an update improves how its format's tags are read. `--found-only` and `--missing-only` filter the plain terminal report; exports always contain the full audit, except TXT, which lists missing tracks.

## Browse incomplete releases

```bash
# Two-panel browser for your organized library (/mnt/music/Library by default)
discogseek browse

# Start with an artist filter or a different library
discogseek browse --artist "Artist" -d /path/to/music

# Inspect matches without submitting downloads
discogseek browse --dry-run -f flac
```

The left pane lists incomplete releases; the right shows every track in the selected release, including disc/track numbers and found, missing, matched, queued, stale, or downloaded status. The browser scans local files under `/mnt/music/Library` (or the folder selected with `-d` or `DISCOGSEEK_BROWSER_LIBRARY_DIR`) and every configured Navidrome/Subsonic album, reusing saved MusicBrainz audits for unchanged releases across launches. Local downloads outside that folder are excluded, including during refreshes and download checks. Configure Navidrome to index the same organized library if unsorted downloads should stay out of the browser. If an album is split across different edition tags, a file tagged with a recording's MusicBrainz ID also counts for the album's other editions: those with the same artist and album title, ignoring spaces and punctuation, or, once audited, the same MusicBrainz release group. A recording that appears once on an edition counts at any position; a repeated one only at its own disc/track position. The first run, and the first after an update that changes how releases are verified, builds this audit cache. New or changed releases are audited as needed; failed audits remain retryable. Use `r` to refresh a selected release from MusicBrainz or launch with `--force-refresh` to refresh all audits. It covers releases already represented in your library; use `download "Artist"` for entirely absent releases.

Verified editions in the same MusicBrainz release group share one album row. Press **e** to choose an edition; the menu shows its format, official track count, release date, country, and missing tracks. The browser initially selects the incomplete edition with the fewest missing tracks and remembers your choice during the session. Complete editions remain available while another edition of the album is incomplete. Tracks, refreshes, and downloads follow the selected edition. Editions with the same artist, album title, and full recording list at the same disc/track positions also share download status; other editions keep their own status. Releases without a verified album identity remain separate.

Press **Enter** for a menu offering **Download all missing tracks** and **Download selected track only**, with previews for either choice. To select an individual track, press **Tab**, then **↑ / ↓**; the arrow beside the track shows the selection. The download shortcuts also stay visible at the bottom of the screen.

| Key | Action |
| --- | --- |
| ↑ / ↓ or j / k | Move through the focused pane |
| Tab or ← / → | Switch panes |
| Page Up / Page Down, Home / End | Move through long lists |
| / | Edit the artist filter; Enter applies, Esc cancels |
| Esc | Clear the applied artist filter |
| Enter | Open download options; ↑ / ↓ chooses, Enter runs, Esc closes |
| e | Choose an album edition; ↑ / ↓ chooses, Enter selects, Esc closes |
| d | Queue a request to download all unqueued missing tracks in this release |
| t | Download the selected missing track |
| p | Preview matches for the release without queueing |
| P | Preview matches for the selected track without queueing |
| r | Rescan and refresh the selected release's MusicBrainz audit |
| R | Rescan the library, reusing saved audits for unchanged releases |
| u | Show or hide releases MusicBrainz could not verify |
| ? | Open keyboard help |
| q | Quit after the current operation finishes |

Artist filtering includes compilation track credits. Searches and audits run in the background, so navigation and filtering stay responsive. Press `d` on several releases to add download requests in order, even while another release is being searched or refreshed. The release list marks requests as searching or waiting, and the header shows the waiting count. Repeated requests for the same release and scope are ignored while already waiting or running; previews and real downloads remain separate. Requests run one at a time between library audits. Each request rechecks the local and remote libraries when it starts and skips tracks already submitted successfully during this browser session, including earlier single-track requests. Submission failures remain retryable. `--dry-run` makes both download shortcuts previews for the entire session. Pressing `q` finishes the current operation and discards waiting requests; files already submitted to slskd continue transferring. Waiting requests are not saved across browser sessions.

Single-track downloads search by the track's artist and title first, then try album searches if needed. Downloading all missing tracks starts with album searches and falls back to searches for the remaining individual tracks, using each track's primary artist without featured guests. Both choices queue only the requested missing tracks.

Library rechecks read and write audio metadata caches in batches. If the first album search leaves missing tracks, its fallback query variants run together. Compilations with a one-word title, or whose title search reaches a search limit, are also searched together with a few of their credited artists, and up to two peer folders that showed part of the release are read in full for the remaining tracks. slskd runs two searches at a time, so discogseek keeps two running and starts the next as soon as one finishes. Busy (HTTP 429) submissions are retried, and progress distinguishes reused, completed, and stopped searches, submission failures, and unreadable results. A failed search is reported as a retryable error rather than as no match. Individual track matches from the same peer share queue requests of up to 50 files.

Downloads queued during the browser session update automatically. The background worker checks slskd every three seconds between operations, prioritizing overdue checks before the next waiting request, and refreshes affected releases when transfers finish. A running search or audit can delay these checks. Rejected, timed-out, errored, or aborted (size-mismatch) transfers get up to two automatic searches for another source per track per browser session; tracks of one release that fail together share one search. A timed-out or errored transfer counts as failed only once slskd's own retries are used up or no retry follows within slskd's longest retry delay; until then, `d`/`t` requests also leave it queued. The browser stops the failed transfer's retries without deleting its history, rechecks the library, and skips previously failed sources. Missing transfer IDs, cancellation failures, no alternatives, or the recovery limit stop automatic recovery and leave `d`/`t` available for a manual retry. A recovery search interrupted by an slskd or Navidrome outage is retried after 1, 2 and 4 minutes. Cancelled transfers become missing again without automatic recovery; cancellations of many transfers at once by slskd do not count against their sources. A transfer still queued `DISCOGSEEK_STALE_QUEUE_HOURS` (default 6) after it was requested or last attempted, from a peer that has sent no file in that time, is shown as `STALE`: the browser searches for another source (one of the two automatic searches), keeps the waiting transfer if none is found, and stops it only after a replacement is queued or the track appears in the library. `d`/`t` search stale tracks again. While transfers wait, the library is rechecked every five minutes. Recovery applies only to downloads tracked during this browser session.

`DOWNLOADED` means the transfer succeeded but the library has not picked up the file yet; the browser rechecks the library every 15 seconds for 10 minutes after a file finishes, then every five minutes, until it becomes `FOUND`, and complete releases leave the incomplete list. Files go to slskd's configured download destination and remain `DOWNLOADED` while they wait there to be moved into the scanned library, for example by Picard, or indexed by Navidrome. A finished file that has left that folder (found through slskd's options or `DISCOGSEEK_SLSKD_DOWNLOADS_DIR`) and is still missing from the library 10 minutes after finishing becomes missing again, so `d`/`t` can retry it from another source. Without access to the folder, this happens after `DISCOGSEEK_IMPORT_WINDOW_HOURS` (default 24). A queued transfer that disappears from slskd's transfer list, with no file in that folder, becomes missing again after the same grace period and can be retried with `d`/`t` from another source. Unverified releases cannot be downloaded from the browser. Browsing itself does not require slskd credentials. The UI uses Python's standard `curses` module on Linux/macOS and needs an interactive terminal of at least 72 columns by 14 rows.

## Download

```bash
# Search and inspect matches without queuing transfers
discogseek download "Artist" --dry-run

# Queue missing discography tracks through slskd
discogseek download "Artist" -f flac

# Fill gaps in one release
discogseek download "Artist" --release "Album" --dry-run
discogseek download "Artist" --release "Album" -f mp3-320
```

Downloads pre-scan the local and configured remote libraries. For compilations, splits, and guest appearances, artist downloads first require one peer's release folder to match the entire official tracklist, including all discs and tracks already in your library. Only then are that release's missing tracks queued. If no complete source is verified, only missing tracks credited to the requested artist's MusicBrainz ID are searched and queued; other artists' partial matches are skipped. Progress reports this fallback, and JSON results identify the `download_scope` as `release` or `artist_tracks`. Standalone recordings are also included. Explicit release and browser downloads still fill requested gaps from available matches. `--dry-run` uses the same selection rules without queueing transfers. Verification checks the available tracklist; subsequent peer or transfer failures can still leave downloads incomplete.

For the artist's own releases, every edition in the MusicBrainz release group counts, so an expanded edition's bonus tracks belong to the album. Folders from different peers, including `CD1`/`CD2` subfolders, can together complete a release; a loose file is accepted only with the artist's name and the exact title and version. Missing tracks that no discovered folder or file supplies are then searched per release and track, with `download_scope` `missing_tracks`. This can add many searches for a large discography, also with `--dry-run`; `--no-track-search` skips them and lists those tracks in `unresolved_tracks`. JSON results list each track still missing in `unresolved_tracks` with a reason, and failed discovery searches in `search_errors`.

Release downloads match the printed or the recording title and reject files whose name or tags credit another artist. In a folder that names the release or holds two of its other tracks, track numbers must agree unless an exact, unrepeated title and its length identify another edition's numbering; flat numbering across discs, disc prefixes such as `201`, and vinyl sides are understood. Elsewhere a file needs the exact title or the same track number, and a clearly different length rules it out. Outside the release's own folder, a copy from a radio capture, DJ mix, podcast, live set, playlist or auto-DJ dump, or mixtape folder (such as `…[SomaFM]` or `Playlist Auto DJ - 2012`) is never used, unless the release or the track is itself a mix, live or radio recording; artists such as Radio Slave and a track titled 'Radio' are unaffected. Another release's 'Intro', 'Interlude' or similar part title is used only when its length matches. Decorative symbols inside names compare equal, so `sato❤shin` matches `sato♥shin`. Filenames that put the title before the credited artist (`7 - KounagiHisyouKa (CM Mix) - Kyou1110.mp3`), use underscores as their only separators (`04_kyou1110_Izumikami.mp3`) or write the position twice (`13 13. Title.mp3`) are read accordingly. A title bracket that only names a credited artist (`Way Back More Back Mix (Kyou1110)`) and an uploader's bracketed note such as `(Commission Song)` or `(Remix pack in Description)` are not part of the title; a song titled like a version tag (`hd`, `Live`) still matches its file, and a masked word (`my fuXX Sound`) matches the spelled-out word. On a single or EP whose remix MusicBrainz titles like the original and credits to the remixer, `Title (Remixer Remix)` matches it. A release title that runs a date into `Live` (`2011/12/11Live at Fukuoka`) counts as a live release.

Downloads show progress on stderr by default: MusicBrainz lookup, library scanning, Soulseek search counts, matching, and queueing. Messages include elapsed time, with a “still working” update during waits of 10 seconds or more. Use `--quiet` (`-q`) to hide progress while keeping the final results and errors. `--json -` keeps stdout machine-readable.

Each search asks slskd to end it 15 seconds after the last new response or at the response or file limit; slskd 0.26 returns responses only once a search has ended. A search still running `--timeout` (default 30, at least 28) plus 45 seconds after slskd started it is stopped and keeps the responses received so far. An identical search slskd finished with files within `DISCOGSEEK_SEARCH_REUSE_MINUTES` is reused, and one still running is joined; one that finished without files is reused within `DISCOGSEEK_EMPTY_SEARCH_REUSE_MINUTES` (default 15) unless it errored or was stopped. `--refresh-searches` runs every search of a download again instead of reusing slskd's recent results; `--force-refresh` does the same. Query variants that differ only in punctuation are searched once. Dashes at the start or end of a word (`-Red Side-`) are left out of the search text, because Soulseek reads `-word` as a word to exclude; an earlier slskd search stored with such an exclusion is not reused. Peers match words inconsistently, so a compilation or guest track that its title searches miss is finally searched as its credited artist with the title's most distinctive words, then as the artist alone; from those broad results only the exact title, or a file in the release's own folder, is taken. In artist downloads, a track MusicBrainz credits to the artist under another spelling (`Kyou` for kyou1110) is also searched and matched under the artist's main name, and a bracketed note in a credit (`kyou1110 (Ghost:…)`) is left out of searches. `--min-match` adjusts the artist workflow's minimum album match ratio (default `0.70`). Queue failures and failed searches for a release's tracks return a nonzero exit status; a successful queue request does not mean the transfer has finished.

When slskd reports an offline peer during queueing, discogseek skips that peer for the rest of the request and tries other matching sources. Explicit peer connection failures get up to three attempts, with 1- and 2-second delays, before falling back. This applies to artist, release, and browser downloads. Busy (HTTP 429) queue requests are retried after 1, 2, and 4 seconds. Files slskd refuses or reports it did not queue are tried from other sources, in artist downloads too, without skipping that peer's other files. Successfully submitted files are preserved, including earlier batches of a partially submitted album. Unexplained server errors and request timeouts remain visible without automatic resubmission, since slskd may already have accepted the files. If no reachable match remains, retry the download later; unavailable peers are reconsidered on the next request.

Download requests also read slskd transfer history. Terminal failures do not count as queued or successful downloads, and the failed file is not chosen again while slskd keeps its record. The effect on the peer depends on slskd's reason. A refusal because you are banned, need verification, or are in a blocked country, and a manual cancellation, block the entire peer from new submissions, including other files and albums, until you unblock it. Other refusals, such as “Too many files” or an overwhelmed peer, pause the peer for `DISCOGSEEK_PEER_COOLDOWN_HOURS` (default 6); afterwards the peer and that file can be chosen again. A file the peer no longer shares or cannot read, a size mismatch (aborted transfer), a timeout, or a generic error excludes only that file. Three or more cancellations within a second of each other come from slskd or a bulk cleanup and count against neither the peer nor its files. Automatic fallback can still choose other peers. Before submitting a replacement, discogseek stops retries of matching failed transfers and preserves their history. It leaves active and successful transfers alone and reports slskd's failure reason and retry count when recovery cannot finish. Preview requests can remember peer blocks but never cancel or enqueue anything. The `download` command ends after queueing: rerun it to recover transfers that fail later, or use the browser for live recovery of its tracked downloads.

Active and successful transfers are checked independently of new search results, so an absent peer does not cause another copy to be queued. History matches require title, version, and artist context, and retain distinct repeated track positions. A transfer still queued `DISCOGSEEK_STALE_QUEUE_HOURS` after it was requested or last attempted no longer holds its track: the next request searches for another source, stops the waiting transfer when it queues a replacement, and leaves it queued when none is found. A successful transfer counts only while its file is still in slskd's downloads folder (`DISCOGSEEK_SLSKD_DOWNLOADS_DIR`, or slskd's own setting when that path exists on this machine) and, in release downloads, only when it came from that release's folder or discogseek submitted it for that very track. discogseek records each file slskd accepts, and each existing transfer it counts for a track, in `submissions.db` inside `DISCOGSEEK_CACHE_DIR`, naming the track by its MusicBrainz track ID (or recording, release, position and title), so a finished file taken from another edition's folder is not downloaded again while it waits for import. A record for a different track, or for a radio or mix folder, does not count; preview runs never write the file, and an unreadable file is ignored. Without access to the folder, it counts for `DISCOGSEEK_IMPORT_WINDOW_HOURS` after finishing. After that, or once transfer history is cleared, duplicate prevention depends on the local and remote library scans; move finished files into the scanned library, or let Navidrome index them, rather than elsewhere.

Peer blocks are saved in `peer_policy.db` inside `DISCOGSEEK_CACHE_DIR` (default `~/.cache/discogseek`) and survive restarts and cleared slskd history. Blocks saved by earlier versions are re-evaluated with these rules: lasting blocks stay, temporary refusals keep at most their remaining cooldown, and file-level refusals are removed. Downloads recheck history before each enqueue batch and connection retry so cancellations observed during a search also block submission. discogseek records its own successful cancellations so recovery cleanup is not mistaken for a manual cancellation. The exemption covers that cancellation event; a later manual cancellation of a resumed transfer can still block the peer. Existing cancelled records without that provenance are treated as manual stops. Only events observed while history is available can be remembered.

List or remove persisted peer blocks, including cooldowns that have not expired (shown with the time they end), without connecting to slskd:

```bash
discogseek peers
discogseek peers unblock "username"
```

Unblocking allows new files from the peer; exact failed files remain excluded while their entries remain in slskd history. Unchanged historical failures do not immediately reblock an explicitly unblocked peer, but a new failure does. The browser also remembers failed source files until it closes. discogseek does not change slskd's retry, networking, or storage settings: slskd may retry transfers already submitted according to its own [retry configuration](https://github.com/slskd/slskd/blob/master/docs/config.md#retry-behavior).

`artist`, `soulseek`, and `slsk` are aliases for `download`. `ds`, `python -m discogseek`, and the repository's `python3 main.py` expose the same commands.

## Scope and code

The web server, browser assets, background task framework, Rich terminal displays, runtime settings editor, external download-link harvesting, audio tag writer, and old compatibility scripts have been removed. The two-pane terminal browser uses the standard library. Configuration comes from the environment or `.env`; downloads go to the location configured in slskd.

```text
src/discogseek/
  cli/          Audit/download commands and the two-pane release browser
  clients/      MusicBrainz, slskd, Navidrome/Subsonic
  core/         Audio metadata, matching, caching, transfer history, peer blocks, report exports
  services/     Artist/release auditing, reconciliation, download selection
```

Run the offline regression suite:

```bash
python3 -m pytest -q
```

Tests use temporary library/cache paths and fake service clients. They cover metadata extraction, multilingual/version matching, multi-disc and vinyl releases, slskd searching, browsing and queueing against a simulated slskd 0.26 server, transfer recovery, and CLI workflows with local and remote libraries.

Tests are grouped by the code and behavior they exercise:

```text
tests/
  cli/          Command parsing and complete audit/download workflows
  clients/      slskd search polling, queue payloads, and directory caching
  core/         Audio metadata, text normalization, versions, and disc numbering
  services/     Library scans, artist/release reconciliation, and download selection
  helpers/      Shared catalog builders and in-memory slskd fakes
  conftest.py   Isolated configuration for every test
```

Run a specific area with `python3 -m pytest tests/services -q`. Name new test files after the behavior they cover, and keep regression cases alongside that behavior.
