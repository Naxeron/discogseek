import { refreshSystemStatus } from '/static/js/dashboard.js';

export async function loadConfig() {
  try {
    const res = await fetch('/api/config');
    if (!res.ok) return;
    const cfg = await res.json();

    const cfgLib = document.getElementById('cfg-lib-dir');
    if (cfgLib) cfgLib.value = cfg.DEFAULT_LIBRARY_DIR || '';
    const cfgOut = document.getElementById('cfg-out-dir');
    if (cfgOut) cfgOut.value = cfg.DEFAULT_OUTPUT_DIR || '';
    const cfgSlskdUrl = document.getElementById('cfg-slskd-url');
    if (cfgSlskdUrl) cfgSlskdUrl.value = cfg.SLSKD_URL || '';
    const cfgSlskdUser = document.getElementById('cfg-slskd-user');
    if (cfgSlskdUser) cfgSlskdUser.value = cfg.SLSKD_USERNAME || '';
    const cfgNavUrl = document.getElementById('cfg-nav-url');
    if (cfgNavUrl) cfgNavUrl.value = cfg.NAVIDROME_URL || '';
    const cfgNavUser = document.getElementById('cfg-nav-user');
    if (cfgNavUser) cfgNavUser.value = cfg.NAVIDROME_USER || cfg.NAVIDROME_USERNAME || '';

    const slskPass = document.getElementById('cfg-slskd-pass');
    if (slskPass) {
      slskPass.placeholder = cfg.has_slskd_password ? '•••••••• (configured in .env)' : '';
    }

    const navToken = document.getElementById('cfg-nav-token');
    if (navToken) {
      navToken.placeholder = (cfg.has_navidrome_token || cfg.has_navidrome_password) ? '•••••••• (configured in .env)' : '';
    }

    // Populate default fields in artist downloader tab
    const artistLib = document.getElementById('artist-dl-lib');
    if (artistLib && !artistLib.value) {
      artistLib.value = cfg.DEFAULT_LIBRARY_DIR || '';
    }
    const artistOut = document.getElementById('artist-dl-output');
    if (artistOut && !artistOut.value) {
      artistOut.value = cfg.DEFAULT_OUTPUT_DIR || '';
    }
  } catch (err) {
    console.error('Error loading config:', err);
  }
}

export function setupSettings() {
  const formSettings = document.getElementById('form-settings');
  if (formSettings) {
    formSettings.addEventListener('submit', async (e) => {
      e.preventDefault();
      const slskdPass = document.getElementById('cfg-slskd-pass').value.trim();
      const navToken = document.getElementById('cfg-nav-token').value.trim();

      const updates = {
        DEFAULT_LIBRARY_DIR: document.getElementById('cfg-lib-dir').value.trim(),
        DEFAULT_OUTPUT_DIR: document.getElementById('cfg-out-dir').value.trim(),
        SLSKD_URL: document.getElementById('cfg-slskd-url').value.trim(),
        SLSKD_USERNAME: document.getElementById('cfg-slskd-user').value.trim(),
        NAVIDROME_URL: document.getElementById('cfg-nav-url').value.trim(),
        NAVIDROME_USER: document.getElementById('cfg-nav-user').value.trim(),
        NAVIDROME_USERNAME: document.getElementById('cfg-nav-user').value.trim(),
      };

      if (slskdPass) {
        updates.SLSKD_PASSWORD = slskdPass;
      }
      if (navToken) {
        updates.NAVIDROME_TOKEN = navToken;
        updates.NAVIDROME_PASSWORD = navToken;
      }

      try {
        const res = await fetch('/api/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(updates),
        });
        if (res.ok) {
          alert('Configuration saved successfully!');
          refreshSystemStatus();
        } else {
          alert('Failed to save settings.');
        }
      } catch (err) {
        alert(`Settings save error: ${err.message}`);
      }
    });
  }
}
