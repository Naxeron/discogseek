import { escapeHtml } from '/static/js/html.js';
import { ansiToHtml } from '/static/js/ansi.js';

export function createTaskController({ switchTab, onTasksUpdated, onTaskFinished }) {
  let tasks = [];
  let selectedTaskId = null;

  function setup() {
    document.getElementById('btn-refresh-task-list').addEventListener('click', refreshTaskList);
    document.getElementById('btn-cancel-task').addEventListener('click', cancelSelectedTask);
    for (const id of ['dash-recent-tasks', 'task-list-sidebar']) {
      document.getElementById(id).addEventListener('click', (event) => {
        const item = event.target.closest('[data-task-id]');
        if (!item) return;
        switchTab('tasks');
        selectTask(item.dataset.taskId);
      });
    }
  }

  async function startTask(taskType, params, name = null) {
    try {
      const res = await fetch('/api/tasks/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type: taskType, params, name })
      });

      if (!res.ok) {
        const err = await res.json();
        alert(`Error starting task: ${err.error || res.statusText}`);
        return null;
      }

      const data = await res.json();
      const task = data.task;

      // Show in global banner
      showGlobalTaskBanner(task);

      // Refresh task list & select it
      await refreshTaskList();
      selectTask(task.id);

      return task;
    } catch (err) {
      alert(`Failed to trigger task: ${err.message}`);
      return null;
    }
  }

  function showGlobalTaskBanner(task) {
    const banner = document.getElementById('global-task-banner');
    const nameEl = document.getElementById('global-task-name');
    banner.classList.remove('hidden');
    nameEl.textContent = `${task.name}: ${task.stage || 'Running'}`;
  }

  function hideGlobalTaskBanner() {
    document.getElementById('global-task-banner').classList.add('hidden');
  }

  async function refreshTaskList() {
    try {
      const res = await fetch('/api/tasks?limit=30');
      if (!res.ok) return;
      const data = await res.json();
      tasks = data.tasks || [];

      const activeCount = tasks.filter((t) => t.status === 'running' || t.status === 'pending').length;
      const badge = document.getElementById('active-tasks-badge');
      if (activeCount > 0) {
        badge.textContent = activeCount;
        badge.style.display = 'inline-block';
      } else {
        badge.style.display = 'none';
        hideGlobalTaskBanner();
      }

      onTasksUpdated(tasks);

      // Render Dashboard Compact List
      renderDashTaskList();

      // Render Task Console Sidebar
      renderTaskSidebar();
    } catch (err) {
      console.error('Error refreshing task list:', err);
    }
  }


  function renderDashTaskList() {
    const container = document.getElementById('dash-recent-tasks');
    if (tasks.length === 0) {
      container.innerHTML = '<div class="text-muted">No recent tasks executed.</div>';
      return;
    }

    container.innerHTML = tasks.slice(0, 5).map((t) => `
      <div class="task-nav-item" data-task-id="${escapeHtml(t.id)}">
        <div class="task-nav-title">${escapeHtml(t.name)}</div>
        <div class="task-nav-meta">
          <span class="badge badge-${t.status}">${t.status}</span>
          <span>${t.created_at ? t.created_at.slice(11, 19) : ''}</span>
        </div>
      </div>
    `).join('');
  }

  function renderTaskSidebar() {
    const container = document.getElementById('task-list-sidebar');
    if (tasks.length === 0) {
      container.innerHTML = '<div class="p-3 text-muted">No tasks available.</div>';
      return;
    }

    container.innerHTML = tasks.map((t) => `
      <div class="task-nav-item ${selectedTaskId === t.id ? 'active' : ''}" data-task-id="${escapeHtml(t.id)}">
        <div class="task-nav-title">${escapeHtml(t.name)}</div>
        <div class="task-nav-meta">
          <span class="badge badge-${t.status}">${t.status}</span>
          <span>${t.created_at ? t.created_at.slice(11, 19) : ''}</span>
        </div>
      </div>
    `).join('');
  }

  let activePollTimer = null;
  let lastRenderedLogCount = 0;

  function selectTask(taskId) {
    selectedTaskId = taskId;
    renderTaskSidebar();

    if (activePollTimer) {
      clearTimeout(activePollTimer);
      activePollTimer = null;
    }

    const logViewer = document.getElementById('terminal-log-viewer');
    logViewer.innerHTML = '';
    lastRenderedLogCount = 0;

    const task = tasks.find((t) => t.id === taskId);
    if (task) {
      document.getElementById('current-task-title').textContent = `${task.name} (${task.type})`;
      updateTaskStatusBadge(task.status);
      document.getElementById('current-task-progress-bar').style.width = `${task.progress || 0}%`;
      document.getElementById('btn-cancel-task').disabled = (task.status !== 'running' && task.status !== 'pending');
    }

    pollTaskLogs(taskId);
  }

  function updateTaskStatusBadge(status) {
    const badge = document.getElementById('current-task-status-badge');
    badge.className = `badge badge-${status}`;
    badge.textContent = status;
  }

  async function pollTaskLogs(taskId) {
    if (selectedTaskId !== taskId) return;
    try {
      const res = await fetch(`/api/tasks/${taskId}?logs=true&log_limit=1000`);
      if (!res.ok) return;
      const task = await res.json();

      // A different task may have been selected while this request was in flight.
      if (selectedTaskId !== taskId) return;

      const logViewer = document.getElementById('terminal-log-viewer');
      const autoscroll = document.getElementById('chk-autoscroll');

      // Incremental log rendering to avoid flickering and preserve text selection
      if (task.logs) {
        if (task.logs.length < lastRenderedLogCount) {
          logViewer.innerHTML = '';
          lastRenderedLogCount = 0;
        }
        if (task.logs.length > lastRenderedLogCount) {
          const newLogs = task.logs.slice(lastRenderedLogCount);
          newLogs.forEach(appendLogLine);
          lastRenderedLogCount = task.logs.length;
          if (autoscroll.checked) {
            logViewer.scrollTop = logViewer.scrollHeight;
          }
        }
      }

      document.getElementById('current-task-progress-bar').style.width = `${task.progress || 0}%`;
      updateTaskStatusBadge(task.status);
      document.getElementById('btn-cancel-task').disabled = (task.status !== 'running' && task.status !== 'pending');

      if (task.status === 'completed' || task.status === 'failed' || task.status === 'cancelled') {
        onTaskFinished(task);
        refreshTaskList();
      } else {
        activePollTimer = setTimeout(() => pollTaskLogs(taskId), 800);
      }
    } catch (err) {
      console.error('Error polling task logs:', err);
    }
  }

  function appendLogLine(entry) {
    const logViewer = document.getElementById('terminal-log-viewer');
    const div = document.createElement('div');
    div.className = 'log-line';

    const timeSpan = document.createElement('span');
    timeSpan.className = 'log-time';
    timeSpan.textContent = `[${entry.time || ''}]`;

    const msgSpan = document.createElement('span');
    if (entry.level === 'ERROR') {
      msgSpan.className = 'log-error';
    } else if (entry.level === 'WARNING') {
      msgSpan.className = 'log-warn';
    } else {
      msgSpan.className = 'log-info';
    }
    msgSpan.innerHTML = ' ' + ansiToHtml(entry.message || '');

    div.appendChild(timeSpan);
    div.appendChild(msgSpan);
    logViewer.appendChild(div);
  }


  async function cancelSelectedTask() {
    if (!selectedTaskId) return;
    try {
      await fetch(`/api/tasks/${selectedTaskId}/cancel`, { method: 'POST' });
      refreshTaskList();
    } catch (err) {
      alert(`Failed to cancel task: ${err.message}`);
    }
  }

  return { setup, startTask, refreshTaskList };
}
