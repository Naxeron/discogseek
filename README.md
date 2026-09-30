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

The cache defaults to `~/.cache/discogseek`. The local library defaults to `/mnt/music` when present, otherwise `./music`. Use `-d` to select a library or a release folder. An absent local directory is treated as empty, allowing remote-only audits or new discography downloads.

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

`--full-scan` inspects all local tags during an artist audit instead of targeting likely files. `--force-refresh` refreshes MusicBrainz metadata; scans reuse metadata cached for unchanged local files. `--found-only` and `--missing-only` filter the plain terminal report; exports always contain the full audit, except TXT, which lists missing tracks.

## Browse incomplete releases

```bash
# Two-panel browser for your whole library
discogseek browse

# Start with an artist filter or a different library
discogseek browse --artist "Artist" -d /path/to/music

# Inspect matches without submitting downloads
discogseek browse --dry-run -f flac
```

The left pane lists incomplete releases; the right shows every track in the selected release, including disc/track numbers and found, missing, matched, or queued status. The browser scans local files and every configured Navidrome/Subsonic album and reuses saved MusicBrainz audits for unchanged releases across launches. If an album is split across different edition tags, tracks with the same MusicBrainz recording ID and disc/track position count across editions with the same artist and album title. The first run builds this audit cache. New or changed releases are audited as needed; failed audits remain retryable. Use `r` to refresh a selected release from MusicBrainz or launch with `--force-refresh` to refresh all audits. It covers releases already represented in your library; use `download "Artist"` for entirely absent releases.

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

Single-track downloads search by the track's artist and title first, then try album searches if needed. Downloading all missing tracks starts with album searches and falls back to searches for the remaining individual tracks, using track artist credits for compilations. Both choices queue only the requested missing tracks.

Library rechecks read and write audio metadata caches in batches. If the first album search leaves missing tracks, its fallback query variants run together; slskd search polling also runs concurrently, with at most eight searches at a time. Individual track matches from the same peer share queue requests of up to 50 files. Searches still wait for completion or the search timeout so late peer responses can be considered.

Downloads queued during the browser session update automatically. The background worker checks slskd every three seconds between operations, prioritizing overdue checks before the next waiting request, and refreshes affected releases when transfers finish. A running search or audit can delay these checks. Rejected, timed-out, or errored transfers get up to two automatic searches for another source per track per browser session. The browser stops the failed transfer's retries without deleting its history, rechecks the library, and skips previously failed sources. Missing transfer IDs, cancellation failures, no alternatives, or the recovery limit stop automatic recovery and leave `d`/`t` available for a manual retry. Cancelled or aborted transfers become missing again without automatic recovery. Healthy transfers, including `Queued, Remotely` with earlier retries, remain queued. Recovery applies only to downloads tracked during this browser session.

`DOWNLOADED` means the transfer succeeded but the library has not picked up the file yet; the browser retries the library check every 15 seconds until it becomes `FOUND`, and complete releases leave the incomplete list. Files go to slskd's configured download destination, so they must be moved into the scanned library or indexed by Navidrome to count as found. Unverified releases cannot be downloaded from the browser. Browsing itself does not require slskd credentials. The UI uses Python's standard `curses` module on Linux/macOS and needs an interactive terminal of at least 72 columns by 14 rows.

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

Downloads pre-scan the local and configured remote libraries. Artist discovery covers primary releases, compilation appearances, and standalone tracks. Partial albums queue only matched missing tracks. `--dry-run` performs searches but never queues transfers. Candidate matching can leave unresolved items; inspect the result before assuming the discography is complete.

Downloads show progress on stderr by default: MusicBrainz lookup, library scanning, Soulseek search counts, matching, and queueing. Messages include elapsed time, with a “still working” update during waits of 10 seconds or more. Use `--quiet` (`-q`) to hide progress while keeping the final results and errors. `--json -` keeps stdout machine-readable.

Use `--timeout` to change the 30-second search timeout and `--min-match` to adjust the artist workflow's minimum album match ratio (default `0.70`). Queue failures return a nonzero exit status; a successful queue request does not mean the transfer has finished.

When slskd reports an offline peer during queueing, discogseek skips that peer for the rest of the request and tries other matching sources. Explicit peer connection failures get up to three attempts, with 1- and 2-second delays, before falling back. This applies to artist, release, and browser downloads. Successfully submitted files are preserved, including earlier batches of a partially submitted album. Unexplained server errors and request timeouts remain visible without automatic resubmission, since slskd may already have accepted the files. If no reachable match remains, retry the download later; unavailable peers are reconsidered on the next request.

Download requests also read slskd transfer history. Terminal failures do not count as queued or successful downloads. Rejected, aborted, or manually cancelled downloads persistently block the entire peer from new submissions, including other files and albums. Timeouts and generic errors exclude the failed file while allowing other files from that peer. Automatic fallback can still choose other peers. Before submitting a replacement, discogseek stops retries of matching failed transfers and preserves their history. It leaves active and successful transfers alone and reports slskd's failure reason and retry count when recovery cannot finish. Preview requests can remember peer blocks but never cancel or enqueue anything. The `download` command ends after queueing: rerun it to recover transfers that fail later, or use the browser for live recovery of its tracked downloads.

Active and successful transfers are checked independently of new search results, so an absent peer does not cause another copy to be queued. History matches require title, version, and artist context, and retain distinct repeated track positions. Once transfer history is cleared, duplicate prevention depends on the local and remote library scans; keep finished files inside the scanned library until they are indexed.

Peer blocks are saved in `peer_policy.db` inside `DISCOGSEEK_CACHE_DIR` (default `~/.cache/discogseek`) and survive restarts and cleared slskd history. Downloads recheck history before each enqueue batch and connection retry so cancellations observed during a search also block submission. discogseek records its own successful cancellations so recovery cleanup is not mistaken for a manual cancellation. The exemption covers that cancellation event; a later manual cancellation of a resumed transfer can still block the peer. Existing cancelled records without that provenance are treated as manual stops. Only events observed while history is available can be remembered.

List or remove persisted peer blocks without connecting to slskd:

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
  core/         Audio metadata, matching, caching, report exports
  services/     Artist/release auditing, reconciliation, download selection
```

Run the offline regression suite:

```bash
python3 -m pytest -q
```

Tests use temporary library/cache paths and fake service clients. They cover metadata extraction, multilingual/version matching, multi-disc releases, slskd polling and queueing, and CLI workflows with local and remote libraries.

Tests are grouped by the code and behavior they exercise:

```text
tests/
  cli/          Command parsing and complete audit/download workflows
  clients/      slskd search polling, queue payloads, and directory caching
  core/         Audio metadata, text normalization, versions, and disc numbering
  services/     Library scans, artist/release reconciliation, and download selection
  helpers/      Shared catalog builders
  conftest.py   Isolated configuration for every test
```

Run a specific area with `python3 -m pytest tests/services -q`. Name new test files after the behavior they cover, and keep regression cases alongside that behavior.
