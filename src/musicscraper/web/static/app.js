import { refreshSystemStatus, refreshTransfers } from '/static/js/dashboard.js';
import { loadConfig, setupSettings } from '/static/js/settings.js';
import { setupArtistDownloader, openArtistDownloader } from '/static/js/artist.js';
import { createTaskController } from '/static/js/tasks.js';
import { createLibraryController } from '/static/js/library.js';

// Feature controllers own their state; this entry point connects their actions.
const tasks = createTaskController({
  switchTab,
  onTasksUpdated(taskList) {
    const activeScan = taskList.find((task) =>
      ['library_scan', 'library_audit_all'].includes(task.type)
      && ['running', 'pending'].includes(task.status)
    );
    library.updateLibraryScanProgress(activeScan);
  },
  onTaskFinished(task) {
    const libraryTasks = [
      'library_scan', 'library_audit', 'library_audit_all',
      'release_missing_download', 'track_soulseek_download',
    ];
    if (task.result && libraryTasks.includes(task.type)) {
      library.loadLibraryReleases();
    }
  },
});

const library = createLibraryController({
  startTask: tasks.startTask,
  switchTab,
  openArtist: (artist) => openArtistDownloader(artist, switchTab),
});

const pageTitles = {
  dashboard: 'System Dashboard',
  releases: 'Library Releases & Missing Track Downloader',
  artist: 'Artist Downloader (Soulseek)',
  tasks: 'Live Task Monitor & Console',
  settings: 'Settings & Integrations',
};

function switchTab(tabId) {
  if (!pageTitles[tabId]) return;

  document.querySelectorAll('.nav-item').forEach((button) => {
    button.classList.toggle('active', button.dataset.tab === tabId);
  });
  document.querySelectorAll('.tab-pane').forEach((pane) => {
    pane.classList.toggle('active', pane.id === `view-${tabId}`);
  });
  document.getElementById('current-page-title').textContent = pageTitles[tabId];

  if (tabId === 'releases') library.ensureLoaded();
}

// Module scripts run after the document has been parsed.
document.querySelectorAll('[data-tab]').forEach((button) => {
  button.addEventListener('click', () => switchTab(button.dataset.tab));
});
document.getElementById('btn-refresh-status').addEventListener('click', () => {
  refreshSystemStatus();
  refreshTransfers();
});
document.getElementById('btn-refresh-transfers').addEventListener('click', refreshTransfers);
document.getElementById('btn-view-global-task').addEventListener('click', () => switchTab('tasks'));

tasks.setup();
setupSettings();
setupArtistDownloader({ startTask: tasks.startTask, switchTab });
library.setupLibraryReleases();

refreshSystemStatus();
loadConfig();
refreshTransfers();
tasks.refreshTaskList();
library.loadLibraryReleases();

setInterval(refreshSystemStatus, 15000);
setInterval(refreshTransfers, 15000);
setInterval(tasks.refreshTaskList, 8000);
