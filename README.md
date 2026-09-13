# MusicScraper

Audit a music library against MusicBrainz and queue missing releases or tracks through Soulseek using slskd. Use the CLI for artist workflows or the web UI to browse library releases, inspect missing tracks, and follow downloads.

## Setup

Requires Python 3.8+ and a running slskd instance for Soulseek searches and downloads.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

Configure services and paths through environment variables or a `.env` file in the project directory:

```dotenv
MUSICSCRAPER_LIBRARY_DIR=/path/to/music
MUSICSCRAPER_CACHE_DIR=/path/to/cache/musicscraper
SLSKD_URL=http://localhost:5030
SLSKD_API_KEY=your-api-key

# Alternatively, authenticate slskd with a username and password:
# SLSKD_USERNAME=your-username
# SLSKD_PASSWORD=your-password

# Optional Navidrome / Subsonic library source for artist audits:
# NAVIDROME_URL=http://localhost:4533
# NAVIDROME_USERNAME=your-username
# NAVIDROME_PASSWORD=your-password
```

The cache defaults to `~/.cache/musicscraper`. The library defaults to `/mnt/music` when present, otherwise `./music`. Download destinations are managed by slskd; MusicScraper queues transfers through its API.

## Usage

The installed `musicscraper` and `ms` commands are equivalent to `python3 main.py` from the repository.

```bash
# Launch the web UI (Library Releases, Artist Downloader, task logs, settings)
musicscraper web --open

# Audit an artist's discography
musicscraper audit "Stellabee" -d /path/to/music --missing-only
musicscraper audit "Stellabee" --json audit.json --csv audit.csv --txt missing.txt

# Preview Soulseek matches, then queue missing releases
musicscraper soulseek "Mekuso" --dry-run
musicscraper soulseek "Mekuso" --format flac

# Audit an artist and search Soulseek for the missing discography
musicscraper artist "96-glass" --dry-run
musicscraper artist "96-glass" -f mp3-320

# See all options for any command
musicscraper --help
musicscraper soulseek --help
```

The web server listens on `127.0.0.1:8080` by default. Library Releases supports scanning, MusicBrainz audits, and downloads of missing releases or individual tracks. Background tasks expose progress, logs, and cancellation.

The root scripts `web_gui.py`, `slskd_scraper.py`, `artist_downloader.py`, and `check_missing_tracks.py` remain compatibility launchers. The supported commands are `audit`, `soulseek` (alias `slsk`), `artist`, and `web` (alias `gui`).

## Code layout

```text
src/musicscraper/
  cli/main.py             CLI parser and command dispatch
  config.py               Environment settings and .env persistence
  clients/                MusicBrainz, slskd, Navidrome, shared HTTP sessions
  core/                   Audio metadata, text matching, cache, report output
    release_metadata.py   Disc/track numbering and artist-credit parsing
  services/
    artist.py             Artist audit/download workflow
    auditor.py            Artist catalog versus local/remote library audit
    reconciler.py         Catalog-to-library track reconciliation
    candidates.py         Soulseek candidate index and title matching
    soulseek.py           Soulseek artist discovery and download queueing
    library.py            Public library service and shared dependencies
    library_scan.py       Local discovery, metadata caching, release grouping
    library_audit.py      Release-to-MusicBrainz tracklist reconciliation
    library_download.py   Missing release and single-track download selection
  web/
    api.py                Public API facade and task registry
    system.py             Service health and runtime settings
    library.py            Browsing and shared release snapshot
    task_handlers.py      Background workflow adapters and progress reporting
    tasks.py              Task lifecycle, cancellation, and log capture
    server.py             HTTP routing, static assets, and SSE
    static/               HTML, CSS, and native JavaScript modules
```

Keep provider requests in `clients`, matching and metadata rules in `core` or the relevant service, and HTTP/task presentation in `web`. Existing imports from `services.library`, `services.soulseek`, and `web.api` remain available.

The frontend uses native ES modules with `app.js` as its entry point; it has no frontend build step. Static assets are included in the Python package.

## Development checks

```bash
python3 -m pytest -q
```

Tests use temporary library/cache paths and dummy service credentials, independent of the developer's `.env`. HTTP integration tests start servers on loopback, so the test environment must allow local sockets. No real slskd or Navidrome account is required.

With Node.js installed, check frontend module loading, navigation wiring, and rendering without installing frontend dependencies:

```bash
node --experimental-vm-modules tests/frontend_smoke.mjs
```

Earlier hardening plans and test reports are retained in [docs/history](docs/history) as historical records.
