import { escapeHtml } from '/static/js/html.js';

export function createScanProgress({ reloadLibrary }) {
  let scanCompleteTimeout = null;

  function updateLibraryScanProgress(task) {
    const card = document.getElementById('lib-scan-progress-card');
    const btnRescan = document.getElementById('btn-rescan-releases');
    const btnAuditAll = document.getElementById('btn-audit-all-releases');

    if (!card) return;

    if (task) {
      if (scanCompleteTimeout) {
        clearTimeout(scanCompleteTimeout);
        scanCompleteTimeout = null;
      }

      card.classList.remove('hidden');
      card.classList.remove('scan-complete');

      const titleEl = document.getElementById('lib-scan-progress-title');
      const pctEl = document.getElementById('lib-scan-progress-pct');
      const fillEl = document.getElementById('lib-scan-progress-fill');
      const subEl = document.getElementById('lib-scan-progress-sub');

      const pct = task.progress || 0;
      if (titleEl) {
        titleEl.innerHTML = `<span class="spinner-inline"></span> ${escapeHtml(task.name || 'Scanning library...')}`;
      }
      if (pctEl) pctEl.textContent = `${pct}%`;
      if (fillEl) fillEl.style.width = `${pct}%`;
      if (subEl) subEl.textContent = task.stage || 'Scanning audio files from disk...';

      if (btnRescan) btnRescan.classList.add('btn-spinning');
      if (btnAuditAll) btnAuditAll.classList.add('btn-spinning');
    } else {
      if (btnRescan) btnRescan.classList.remove('btn-spinning');
      if (btnAuditAll) btnAuditAll.classList.remove('btn-spinning');

      // If card was showing active progress, show completion state before hiding
      if (!card.classList.contains('hidden') && !card.classList.contains('scan-complete')) {
        card.classList.add('scan-complete');
        const titleEl = document.getElementById('lib-scan-progress-title');
        const pctEl = document.getElementById('lib-scan-progress-pct');
        const fillEl = document.getElementById('lib-scan-progress-fill');
        const subEl = document.getElementById('lib-scan-progress-sub');

        if (titleEl) titleEl.innerHTML = '✔ Scan Complete';
        if (pctEl) pctEl.textContent = '100%';
        if (fillEl) fillEl.style.width = '100%';
        if (subEl) subEl.textContent = 'Library releases up to date.';

        // Reload releases smoothly without flickering
        reloadLibrary();

        scanCompleteTimeout = setTimeout(() => {
          card.classList.add('hidden');
        }, 3000);
      }
    }
  }

  return updateLibraryScanProgress;
}
