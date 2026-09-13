import { escapeHtml } from '/static/js/html.js';
import {
  renderReleasesList, renderReleaseDetails,
  updateSelectedReleaseHighlight, updateReleaseListItemDOM,
} from '/static/js/release-view.js';
import { createScanProgress } from '/static/js/scan-progress.js';

export function createLibraryController({ startTask, switchTab, openArtist }) {
  const state = {
    libraryReleases: [],
    selectedReleaseId: null,
    selectedReleaseData: null,
    releaseFilter: 'all',
    releaseSearchQuery: '',
    releaseSortBy: 'artist',
  };

  const updateLibraryScanProgress = createScanProgress({
    reloadLibrary: () => loadLibraryReleases(false),
  });

  function renderList(preserveScroll = true) {
    renderReleasesList(state.libraryReleases, state.selectedReleaseId, state.releaseSortBy, preserveScroll);
  }

  function setupLibraryReleases() {
    document.getElementById('releases-master-list').addEventListener('click', (event) => {
      const item = event.target.closest('[data-release-id]');
      if (item) selectLibraryRelease(item.dataset.releaseId);
    });
    document.getElementById('tbody-release-tracks').addEventListener('click', (event) => {
      const button = event.target.closest('[data-download-track]');
      const release = state.selectedReleaseData;
      if (!button || !release) return;
      const index = Number(button.dataset.downloadTrack);
      const track = release.tracks?.[index];
      if (!track || track.status !== 'missing') return;
      downloadSingleMissingTrack(
        release.artist, release.title, track.title, track.artist || '',
        track.track_number || track.track_num_int || (index + 1),
      );
    });

    // Rescan button
    const btnRescan = document.getElementById('btn-rescan-releases');
    if (btnRescan) {
      btnRescan.addEventListener('click', async () => {
        const task = await startTask('library_scan', { force_rescan: true }, 'Rescan Music Library');
        if (task) {
          updateLibraryScanProgress(task);
        }
      });
    }

    // Audit All MB button
    const btnAuditAll = document.getElementById('btn-audit-all-releases');
    if (btnAuditAll) {
      btnAuditAll.addEventListener('click', async () => {
        const task = await startTask('library_audit_all', { force_refresh: true }, 'Audit All Releases (MusicBrainz)');
        if (task) {
          updateLibraryScanProgress(task);
        }
      });
    }

    // Filter buttons
    document.querySelectorAll('[data-filter-release]').forEach((btn) => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('[data-filter-release]').forEach((b) => b.classList.remove('active'));
        btn.classList.add('active');
        state.releaseFilter = btn.getAttribute('data-filter-release');
        loadLibraryReleases(false);
      });
    });

    // Search input
    const searchInput = document.getElementById('lib-release-search');
    const clearBtn = document.getElementById('btn-clear-release-search');

    if (searchInput) {
      let searchDebounce = null;
      searchInput.addEventListener('input', (e) => {
        state.releaseSearchQuery = e.target.value;
        if (clearBtn) {
          clearBtn.classList.toggle('hidden', !e.target.value);
        }
        if (searchDebounce) clearTimeout(searchDebounce);
        searchDebounce = setTimeout(() => {
          loadLibraryReleases(false);
        }, 250);
      });
    }

    if (clearBtn) {
      clearBtn.addEventListener('click', () => {
        if (searchInput) searchInput.value = '';
        clearBtn.classList.add('hidden');
        state.releaseSearchQuery = '';
        loadLibraryReleases(false);
      });
    }

    // Sort dropdown
    const sortSelect = document.getElementById('lib-release-sort');
    if (sortSelect) {
      sortSelect.addEventListener('change', (e) => {
        state.releaseSortBy = e.target.value;
        renderList();
      });
    }

    // Release action buttons
    const btnDownloadMissing = document.getElementById('btn-rel-download-missing');
    if (btnDownloadMissing) {
      btnDownloadMissing.addEventListener('click', () => {
        if (state.selectedReleaseData) {
          downloadMissingForRelease(state.selectedReleaseData);
        }
      });
    }

    const btnAuditMB = document.getElementById('btn-rel-audit-mb');
    if (btnAuditMB) {
      btnAuditMB.addEventListener('click', () => {
        auditSelectedRelease();
      });
    }

    const btnSearchSlsk = document.getElementById('btn-rel-search-soulseek');
    if (btnSearchSlsk) {
      btnSearchSlsk.addEventListener('click', () => {
        searchSoulseekForSelectedRelease();
      });
    }
  }

  async function loadLibraryReleases(refresh = false) {
    const masterList = document.getElementById('releases-master-list');
    if (!masterList) return;

    if (refresh) {
      masterList.innerHTML = '<div class="empty-state-card"><div class="spinner-inline"></div><div class="text-muted mt-2">Scanning library on disk...</div></div>';
    }

    try {
      const queryParams = new URLSearchParams({
        refresh: refresh ? 'true' : 'false',
        search: state.releaseSearchQuery || '',
        filter: state.releaseFilter || 'all',
      });
      const res = await fetch(`/api/library/releases?${queryParams.toString()}`);
      if (!res.ok) return;
      const data = await res.json();

      state.libraryReleases = data.releases || [];

      // Update Summary Header Cards
      if (data.summary) {
        const elTotal = document.getElementById('lib-rel-total-count');
        const elComp = document.getElementById('lib-rel-complete-count');
        const elMiss = document.getElementById('lib-rel-missing-count');
        const elTrkTotal = document.getElementById('lib-rel-tracks-total');
        const elMissSub = document.getElementById('lib-rel-missing-tracks-sub');

        if (elTotal) elTotal.textContent = data.summary.total_releases || 0;
        if (elComp) elComp.textContent = data.summary.complete_releases || 0;
        if (elMiss) elMiss.textContent = data.summary.has_missing_releases || 0;
        if (elTrkTotal) elTrkTotal.textContent = `${data.summary.total_local_tracks || 0} local tracks`;
        if (elMissSub) elMissSub.textContent = `${data.summary.total_missing_tracks || 0} missing tracks`;
      }

      renderList();

      // If an existing release was selected, ensure it remains rendered and updated with new data
      if (state.selectedReleaseId) {
        let updated = (data.releases || []).find((r) => r.id === state.selectedReleaseId);
        if (!updated && state.selectedReleaseData) {
          // Fallback match by title and artist in case ID shifted due to unification
          updated = (data.releases || []).find(
            (r) => r.title === state.selectedReleaseData.title && r.artist === state.selectedReleaseData.artist
          );
        }
        if (updated) {
          state.selectedReleaseId = updated.id;
          state.selectedReleaseData = updated;
          renderReleaseDetails(updated);
          // Refresh full details in background to ensure tracks match latest disk scan
          selectLibraryRelease(updated.id);
        } else if (state.selectedReleaseData) {
          renderReleaseDetails(state.selectedReleaseData);
        }
      }
    } catch (err) {
      console.error('Error loading library releases:', err);
      masterList.innerHTML = `<div class="empty-state-card text-red">Failed to load releases: ${escapeHtml(err.message)}</div>`;
    }
  }

  async function selectLibraryRelease(releaseId, forceAudit = false) {
    state.selectedReleaseId = releaseId;
    updateSelectedReleaseHighlight(releaseId);

    const placeholder = document.getElementById('release-empty-placeholder');
    const detailWrapper = document.getElementById('release-detail-wrapper');

    if (placeholder) placeholder.classList.add('hidden');
    if (detailWrapper) detailWrapper.classList.remove('hidden');

    // If we already have full data for this release in memory, render that immediately
    let currentRel = null;
    if (state.selectedReleaseData && state.selectedReleaseData.id === releaseId) {
      currentRel = state.selectedReleaseData;
    } else {
      currentRel = state.libraryReleases.find((r) => r.id === releaseId);
    }

    if (currentRel) {
      state.selectedReleaseData = currentRel;
      renderReleaseDetails(currentRel);
    }

    // Check if release has placeholder titles like "Track 01 (Missing)" or hasn't been audited
    const hasPlaceholders = (currentRel?.tracks || []).some((t) =>
      /^(?:Disc\s+\d+\s+)?Track\s+\d+\s*\(Missing\)$/i.test(t.title || '')
    );
    const isAudited = Boolean(currentRel?.is_audited);
    const shouldAudit =
      forceAudit ||
      !isAudited ||
      hasPlaceholders ||
      !currentRel ||
      !currentRel.tracks ||
      currentRel.tracks.length === 0;

    if (shouldAudit) {
      const btnAuditMB = document.getElementById('btn-rel-audit-mb');
      if (btnAuditMB && (hasPlaceholders || !isAudited)) {
        btnAuditMB.disabled = true;
        btnAuditMB.textContent = 'Auditing MusicBrainz...';
      }

      try {
        const res = await fetch(`/api/library/releases/${encodeURIComponent(releaseId)}?audit=true`);
        if (!res.ok) return;
        const releaseData = await res.json();
        if (state.selectedReleaseId === releaseId) {
          state.selectedReleaseData = releaseData;
          renderReleaseDetails(releaseData);
        }

        // Sync master list release object in memory and update DOM element in place (NO list jumping or re-sorting)
        const idx = state.libraryReleases.findIndex((r) => r.id === releaseId);
        if (idx !== -1) {
          state.libraryReleases[idx] = { ...state.libraryReleases[idx], ...releaseData };
          updateReleaseListItemDOM(state.libraryReleases[idx], state.selectedReleaseId);
        }
      } catch (err) {
        console.error('Error fetching release details:', err);
      } finally {
        if (btnAuditMB) {
          btnAuditMB.disabled = false;
          btnAuditMB.textContent = '🔍 Re-Audit with MusicBrainz';
        }
      }
    }
  }

  async function downloadMissingForRelease(rel) {
    if (!rel) return;
    // Prefer audited details so downloads use accurate track titles.
    let currentRel = rel;
    if (state.selectedReleaseData && state.selectedReleaseData.id === rel.id && state.selectedReleaseData.tracks) {
      currentRel = state.selectedReleaseData;
    }
    const missingTracks = (currentRel.tracks || []).filter((t) => t.status === 'missing');
    if (missingTracks.length === 0) {
      alert('All tracks for this release are already present in the library!');
      return;
    }

    switchTab('tasks');
    await startTask('release_missing_download', {
      artist: currentRel.artist,
      release_title: currentRel.title,
      missing_tracks: missingTracks,
      format: 'flac',
    }, `Download Missing: ${currentRel.artist} - ${currentRel.title}`);
  }

  async function downloadSingleMissingTrack(artist, releaseTitle, trackTitle, trackArtist = '', trackNumber = null) {
    // If trackTitle is still a generic placeholder, check if audited data is available
    if (/^Track\s+\d+\s*\(Missing\)$/i.test(trackTitle) && state.selectedReleaseData && state.selectedReleaseData.tracks) {
      const match = state.selectedReleaseData.tracks.find(
        (t) => String(t.track_number) === String(trackNumber) || String(t.track_num_int) === String(trackNumber)
      );
      if (match && match.title && !/^Track\s+\d+\s*\(Missing\)$/i.test(match.title)) {
        trackTitle = match.title;
        if (!trackArtist && match.artist) trackArtist = match.artist;
      }
    }

    switchTab('tasks');
    const displayName = trackArtist ? `${trackArtist} - ${trackTitle}` : `${artist} - ${trackTitle}`;
    await startTask('track_soulseek_download', {
      artist,
      release_title: releaseTitle,
      track_title: trackTitle,
      track_artist: trackArtist,
      track_number: trackNumber,
      format: 'flac',
    }, `Download Track: ${displayName}`);
  }

  function searchSoulseekForSelectedRelease() {
    if (!state.selectedReleaseData) return;
    openArtist(state.selectedReleaseData.artist);
  }

  async function auditSelectedRelease() {
    if (!state.selectedReleaseId) return;
    const relId = state.selectedReleaseId;
    const btn = document.getElementById('btn-rel-audit-mb');
    if (btn) {
      btn.disabled = true;
      btn.textContent = 'Auditing...';
    }

    try {
      const res = await fetch(`/api/library/releases/${relId}/audit`, { method: 'POST' });
      if (res.ok) {
        const data = await res.json();
        if (data.release) {
          state.selectedReleaseId = data.release.id;
          state.selectedReleaseData = data.release;
          renderReleaseDetails(data.release);
          // Refresh master list item
          const idx = state.libraryReleases.findIndex((r) => r.id === data.release.id || r.id === relId);
          if (idx !== -1) {
            state.libraryReleases[idx] = data.release;
            updateReleaseListItemDOM(data.release, state.selectedReleaseId);
          } else {
            state.libraryReleases.push(data.release);
            renderList(true);
          }
        }
      } else {
        const errData = await res.json().catch(() => ({}));
        alert(`Audit error: ${errData.error || res.statusText}`);
      }
    } catch (err) {
      alert(`Audit error: ${err.message}`);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = '🔍 Re-Audit with MusicBrainz';
      }
    }
  }

  function ensureLoaded() {
    if (state.libraryReleases.length === 0) loadLibraryReleases();
  }

  return { setupLibraryReleases, loadLibraryReleases, updateLibraryScanProgress, ensureLoaded };
}
