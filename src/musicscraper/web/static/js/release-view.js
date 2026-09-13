import { escapeHtml } from '/static/js/html.js';

export function renderReleaseListItemHtml(r, selectedReleaseId) {
  const isComplete = r.status === 'complete' || (r.missing_count === 0 && r.found_count > 0);
  const badgeClass = isComplete ? 'badge-found' : 'badge-missing';
  const missingText = r.missing_count > 0 ? `${r.missing_count} missing` : (isComplete ? 'Complete' : `${r.found_count} tracks`);
  const isSelected = selectedReleaseId === r.id;

  return `
    <div class="release-list-item ${isSelected ? 'active' : ''}" data-release-id="${escapeHtml(r.id)}">
      <div class="release-item-art">💿</div>
      <div class="release-item-details">
        <div class="release-item-title">${escapeHtml(r.title)}</div>
        <div class="release-item-artist">${escapeHtml(r.artist)} ${r.year ? `(${r.year})` : ''}</div>
        <div class="release-item-tags">
          <span class="badge ${badgeClass}">${missingText}</span>
          <span class="badge font-mono">${r.found_count}${r.total_tracks_expected > r.found_count ? ` / ${r.total_tracks_expected}` : ''} trks</span>
          ${(r.formats || []).map((f) => `<span class="badge">${escapeHtml(f)}</span>`).join('')}
        </div>
      </div>
    </div>
  `;
}

export function updateSelectedReleaseHighlight(releaseId) {
  const masterList = document.getElementById('releases-master-list');
  if (!masterList) return;
  const items = masterList.querySelectorAll('.release-list-item');
  items.forEach((item) => {
    if (item.getAttribute('data-release-id') === releaseId) {
      item.classList.add('active');
    } else {
      item.classList.remove('active');
    }
  });
}

export function updateReleaseListItemDOM(rel, selectedReleaseId) {
  if (!rel || !rel.id) return;
  const masterList = document.getElementById('releases-master-list');
  if (!masterList) return;
  const item = Array.from(masterList.querySelectorAll('[data-release-id]'))
    .find((element) => element.dataset.releaseId === rel.id);
  if (item) {
    const tempDiv = document.createElement('div');
    tempDiv.innerHTML = renderReleaseListItemHtml(rel, selectedReleaseId).trim();
    const newEl = tempDiv.firstElementChild;
    if (newEl) {
      item.replaceWith(newEl);
    }
  }
}

export function renderReleasesList(releases, selectedReleaseId, sortBy = 'artist', preserveScroll = true) {
  const masterList = document.getElementById('releases-master-list');
  if (!masterList) return;

  const prevScrollTop = preserveScroll ? masterList.scrollTop : 0;

  if (!releases || releases.length === 0) {
    masterList.innerHTML = '<div class="empty-state-card"><div class="text-muted">No releases matching current filters.</div></div>';
    return;
  }

  // Sort releases deterministically with stable tie-breakers
  const sorted = [...releases].sort((a, b) => {
    let cmp = 0;
    if (sortBy === 'title') {
      cmp = (a.title || '').localeCompare(b.title || '', undefined, { sensitivity: 'base' });
    } else if (sortBy === 'year') {
      cmp = (b.year || '').localeCompare(a.year || '');
    } else if (sortBy === 'missing') {
      cmp = (b.missing_count || 0) - (a.missing_count || 0);
    } else if (sortBy === 'tracks') {
      cmp = (b.found_count || 0) - (a.found_count || 0);
    } else {
      cmp = (a.artist || '').localeCompare(b.artist || '', undefined, { sensitivity: 'base' });
    }
    if (cmp !== 0) return cmp;
    // Tie-breaker 1: Artist
    const artCmp = (a.artist || '').localeCompare(b.artist || '', undefined, { sensitivity: 'base' });
    if (artCmp !== 0) return artCmp;
    // Tie-breaker 2: Title
    const titleCmp = (a.title || '').localeCompare(b.title || '', undefined, { sensitivity: 'base' });
    if (titleCmp !== 0) return titleCmp;
    // Tie-breaker 3: ID
    return (a.id || '').localeCompare(b.id || '');
  });

  masterList.innerHTML = sorted.map((r) => renderReleaseListItemHtml(r, selectedReleaseId)).join('');

  if (preserveScroll) {
    masterList.scrollTop = prevScrollTop;
  }
}

export function renderReleaseDetails(rel) {
  const elTitle = document.getElementById('rel-det-title');
  const elArtist = document.getElementById('rel-det-artist');
  const elYear = document.getElementById('rel-det-year');
  const elPath = document.getElementById('rel-det-path');
  const elMbid = document.getElementById('rel-det-mbid');

  if (elTitle) elTitle.textContent = rel.title || 'Unknown Title';
  if (elArtist) elArtist.textContent = rel.artist || 'Unknown Artist';
  if (elYear) elYear.textContent = rel.year ? `Year: ${rel.year}` : 'Year: -';
  if (elPath) elPath.textContent = `Folder: ${rel.folder_path || '-'}`;

  if (elMbid) {
    if (rel.mb_release_id) {
      elMbid.innerHTML = `<a href="https://musicbrainz.org/release/${rel.mb_release_id}" target="_blank" class="text-cyan">${rel.mb_release_id.slice(0, 8)}... ↗</a>`;
    } else {
      elMbid.textContent = 'Unlinked (Local match)';
    }
  }

  const isComplete = rel.status === 'complete' || (rel.missing_count === 0 && rel.found_count > 0);
  const statusBadge = document.getElementById('rel-det-badge-status');
  if (statusBadge) {
    statusBadge.className = `badge ${isComplete ? 'badge-found' : 'badge-missing'}`;
    statusBadge.textContent = isComplete ? '100% Complete' : `${rel.missing_count} Missing Tracks`;
  }

  const total = rel.total_tracks_expected || (rel.tracks ? rel.tracks.length : rel.found_count);
  const found = rel.found_count || (rel.tracks ? rel.tracks.filter((t) => t.status === 'found').length : 0);
  const pct = rel.completion_pct !== undefined ? rel.completion_pct : (total > 0 ? ((found / total) * 100).toFixed(0) : 100);

  const elTrackCounts = document.getElementById('rel-det-track-counts');
  const elMissHigh = document.getElementById('rel-det-missing-highlight');
  const elProgFill = document.getElementById('rel-det-progress-fill');

  if (elTrackCounts) elTrackCounts.textContent = `${found} / ${total} tracks (${pct}%)`;
  if (elMissHigh) {
    elMissHigh.textContent = rel.missing_count > 0 ? `${rel.missing_count} missing` : 'All tracks found';
    elMissHigh.className = rel.missing_count > 0 ? 'text-red' : 'text-green';
  }
  if (elProgFill) elProgFill.style.width = `${pct}%`;

  // Download All Missing Tracks button visibility/state
  const btnDownloadAll = document.getElementById('btn-rel-download-missing');
  const missingTracks = (rel.tracks || []).filter((t) => t.status === 'missing');
  if (btnDownloadAll) {
    btnDownloadAll.disabled = missingTracks.length === 0;
    btnDownloadAll.textContent = missingTracks.length > 0 ? `⚡ Download All Missing (${missingTracks.length})` : '✔ All Tracks Downloaded';
  }

  // Format pills
  const formatList = document.getElementById('rel-det-formats-list');
  if (formatList) {
    formatList.innerHTML = (rel.formats || []).map((f) => `<span class="badge font-mono">${escapeHtml(f)}</span>`).join('');
  }

  // Tracks Table
  const tbody = document.getElementById('tbody-release-tracks');
  if (!tbody) return;

  const tracks = rel.tracks || [];

  if (tracks.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">No track data available. Click "Re-Audit with MusicBrainz" to fetch official tracklist.</td></tr>';
    return;
  }

  tbody.innerHTML = tracks.map((t, idx) => {
    const isFound = t.status === 'found';
    const trClass = isFound ? '' : 'track-row-missing';
    const titleClass = isFound ? 'track-title-found' : 'track-title-missing';
    const statusBadgeHtml = isFound
      ? `<span class="badge badge-found">✔ Found</span>`
      : `<span class="badge badge-missing">✖ Missing</span>`;

    const formatInfo = isFound
      ? `${escapeHtml(t.format || 'AUDIO')}${t.bitrate ? ` • ${t.bitrate} kbps` : ''}`
      : '<span class="text-muted">-</span>';

    const detailsInfo = isFound
      ? `<span class="text-truncate font-mono font-xs" style="max-width: 220px; display: inline-block;">${escapeHtml(t.filename || '-')}</span>`
      : '<span class="text-muted font-xs">Not in local library</span>';

    const showArtistInTitle = t.artist && (rel.artist.toLowerCase() === 'various artists' || t.artist.toLowerCase() !== rel.artist.toLowerCase());
    const displayTitle = showArtistInTitle
      ? `<span class="text-muted font-mono font-xs" style="margin-right: 4px;">${escapeHtml(t.artist)} -</span><strong class="${titleClass}">${escapeHtml(t.title)}</strong>`
      : `<strong class="${titleClass}">${escapeHtml(t.title)}</strong>`;

    const actionButton = !isFound
      ? `<button class="btn btn-xs btn-primary" data-download-track="${idx}">⚡ Download</button>`
      : '<span class="text-green font-xs font-mono">✔ In Library</span>';

    return `
      <tr class="${trClass}">
        <td><span class="text-muted font-mono">${t.track_number || (idx + 1)}</span></td>
        <td>${displayTitle}</td>
        <td>${statusBadgeHtml}</td>
        <td>${formatInfo}</td>
        <td>${detailsInfo}</td>
        <td style="text-align: right;">${actionButton}</td>
      </tr>
    `;
  }).join('');
}
