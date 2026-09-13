import { escapeHtml } from '/static/js/html.js';

export async function refreshSystemStatus() {
  try {
    const res = await fetch('/api/status');
    if (!res.ok) return;
    const data = await res.json();

    // Dashboard Cards
    const slsk = data.services?.slskd;
    const nav = data.services?.navidrome;
    const paths = data.paths;

    // slskd
    const slskEl = document.getElementById('dash-slskd-status');
    const slskUserEl = document.getElementById('dash-slskd-user');
    const pillSlsk = document.getElementById('pill-slskd');
    if (slskEl && slskUserEl && pillSlsk) {
      if (slsk?.connected) {
        slskEl.textContent = 'Connected';
        slskEl.className = 'stat-value text-green';
        slskUserEl.textContent = `User: ${slsk.username || 'active'}`;
        pillSlsk.querySelector('.status-dot').className = 'status-dot online';
      } else {
        slskEl.textContent = slsk?.configured ? 'Offline' : 'Not Configured';
        slskEl.className = 'stat-value text-muted';
        slskUserEl.textContent = slsk?.error ? slsk.error.slice(0, 30) : 'Check SLSKD_URL';
        pillSlsk.querySelector('.status-dot').className = 'status-dot offline';
      }
    }

    // Navidrome
    const navEl = document.getElementById('dash-nav-status');
    const navUrlEl = document.getElementById('dash-nav-url');
    const pillNav = document.getElementById('pill-navidrome');
    if (navEl && navUrlEl && pillNav) {
      if (nav?.connected) {
        navEl.textContent = 'Connected';
        navEl.className = 'stat-value text-green';
        navUrlEl.textContent = nav.url || 'Online';
        pillNav.querySelector('.status-dot').className = 'status-dot online';
      } else {
        navEl.textContent = nav?.configured ? 'Offline' : 'Not Configured';
        navEl.className = 'stat-value text-muted';
        navUrlEl.textContent = nav?.error || nav?.url || 'Configure in Settings';
        pillNav.querySelector('.status-dot').className = 'status-dot offline';
      }
    }

    // Library Path
    const libEl = document.getElementById('dash-lib-status');
    const libSubEl = document.getElementById('dash-lib-exists');
    if (libEl && libSubEl) {
      libEl.textContent = paths?.library_dir || '-';
      libSubEl.textContent = paths?.library_exists ? '✔ Directory Verified' : '⚠ Directory not found';
      libSubEl.className = paths?.library_exists ? 'stat-sub text-green' : 'stat-sub text-yellow';
    }
  } catch (err) {
    console.error('Error fetching system status:', err);
  }
}

export async function refreshTransfers() {
  try {
    const res = await fetch('/api/slskd/transfers');
    if (!res.ok) return;
    const data = await res.json();
    const tbody = document.querySelector('#table-slskd-transfers tbody');

    if (!data.connected || !data.downloads || data.downloads.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">No active transfers or slskd not connected.</td></tr>';
      return;
    }

    let rows = '';
    for (const dl of data.downloads) {
      const sizeMb = dl.size ? (dl.size / (1024 * 1024)).toFixed(1) + ' MB' : '-';
      const speedKb = dl.speed ? (dl.speed / 1024).toFixed(0) + ' KB/s' : '0 KB/s';
      const pct = dl.percent ? `${dl.percent.toFixed(0)}%` : '0%';
      rows += `
        <tr>
          <td><span class="peer-user">${escapeHtml(dl.username || dl.remoteUser || '-')}</span></td>
          <td class="text-truncate" style="max-width: 300px;">${escapeHtml(dl.filename || dl.file || '-')}</td>
          <td>${sizeMb}</td>
          <td>${speedKb}</td>
          <td><span class="badge badge-running">${escapeHtml(dl.state || 'active')}</span></td>
          <td>
            <div style="width: 100px; background: #222; height: 6px; border-radius: 3px; overflow: hidden;">
              <div style="width: ${pct}; background: var(--accent-blue); height: 100%;"></div>
            </div>
          </td>
        </tr>
      `;
    }
    tbody.innerHTML = rows;
  } catch (err) {
    console.error('Error fetching transfers:', err);
  }
}
