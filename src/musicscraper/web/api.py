"""Public web API and task registry. HTTP routes depend only on this facade."""

from typing import Any, Dict, Optional

from musicscraper.web.library import (
    browse_library,
    get_library_releases,
    get_library_release_details,
)
from musicscraper.web.system import (
    get_system_status,
    get_system_config,
    update_system_config,
    get_slskd_transfers,
)
from musicscraper.web.task_handlers import (
    run_audit_task,
    run_soulseek_search_task,
    run_soulseek_queue_task,
    run_artist_download_task,
    run_library_scan_task,
    run_release_missing_download_task,
    run_track_soulseek_download_task,
    run_library_audit_all_task,
)
from musicscraper.web.tasks import BackgroundTask, global_task_manager


TASK_DISPATCHER = {
    "audit": run_audit_task,
    "soulseek_search": run_soulseek_search_task,
    "soulseek_download": run_soulseek_queue_task,
    "artist_download": run_artist_download_task,
    "library_scan": run_library_scan_task,
    "library_audit": run_library_audit_all_task,
    "library_audit_all": run_library_audit_all_task,
    "release_missing_download": run_release_missing_download_task,
    "track_soulseek_download": run_track_soulseek_download_task,
}


def launch_task(task_type: str, params: Optional[Dict[str, Any]] = None, name: Optional[str] = None) -> BackgroundTask:
    """Dispatches and launches an asynchronous task."""
    if not isinstance(task_type, str) or not task_type.strip():
        raise ValueError("Task type must be a non-empty string")
    if params is not None and not isinstance(params, dict):
        raise ValueError("Task params must be a dictionary")
    params = params or {}
    fn = TASK_DISPATCHER.get(task_type)
    if not fn:
        raise ValueError(f"Unknown task type: {task_type}")

    friendly_name = name or f"{task_type.replace('_', ' ').title()}"
    return global_task_manager.submit(
        name=friendly_name,
        task_type=task_type,
        target_fn=fn,
        params=params
    )
