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

The left pane lists incomplete releases; the right shows every track in the selected release, including disc/track numbers and found, missing, matched, or queued status. The browser scans local files and every configured Navidrome/Subsonic album and reuses saved MusicBrainz audits for unchanged releases across launches. The first run builds this audit cache. New or changed releases are audited as needed; failed audits remain retryable. Use `r` to refresh a selected release from MusicBrainz or launch with `--force-refresh` to refresh all audits. It covers releases already represented in your library; use `download "Artist"` for entirely absent releases.

Press **Enter** for a menu offering **Download all missing tracks** and **Download selected track only**, with previews for either choice. To select an individual track, press **Tab**, then **↑ / ↓**; the arrow beside the track shows the selection. The download shortcuts also stay visible at the bottom of the screen.

| Key | Action |
| --- | --- |
| ↑ / ↓ or j / k | Move through the focused pane |
| Tab or ← / → | Switch panes |
| Page Up / Page Down, Home / End | Move through long lists |
| / | Edit the artist filter; Enter applies, Esc cancels |
| Esc | Clear the applied artist filter |
| Enter | Open download options; ↑ / ↓ chooses, Enter runs, Esc closes |
| d | Download all unqueued missing tracks in this release |
| t | Download the selected missing track |
| p | Preview matches for the release without queueing |
| P | Preview matches for the selected track without queueing |
| r | Rescan and refresh the selected release's MusicBrainz audit |
| R | Rescan the library, reusing saved audits for unchanged releases |
| u | Show or hide releases MusicBrainz could not verify |
| ? | Open keyboard help |
| q | Quit after the current operation finishes |

Artist filtering includes compilation track credits. Searches and audits run in the background, so navigation and filtering stay responsive. Selected downloads and refreshes run between release audits, then the library scan resumes. Downloads recheck the local and remote libraries first and skip tracks successfully queued during this browser session. Queue failures remain retryable. `--dry-run` makes both download shortcuts previews for the entire session.

Single-track downloads search by the track's artist and title first, then try album searches if needed. Downloading all missing tracks starts with album searches and falls back to searches for the remaining individual tracks, using track artist credits for compilations. Both choices queue only the requested missing tracks.

Queued tracks remain incomplete until files arrive in the library: press `r` to pick up completed transfers. Files go to slskd's configured download destination. Unverified releases cannot be downloaded from the browser. Browsing itself does not require slskd credentials. The UI uses Python's standard `curses` module on Linux/macOS and needs an interactive terminal of at least 72 columns by 14 rows.

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

Use `--timeout` to change the 30-second search timeout, `--min-match` to adjust the artist workflow's minimum album match ratio (default `0.70`), and `--verbose` for progress logs on stderr. `--json -` keeps stdout machine-readable. Queue failures return a nonzero exit status; a successful queue request does not mean the transfer has finished.

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
