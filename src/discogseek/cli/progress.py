"""Download progress on stderr, including a heartbeat during blocking calls."""

from contextlib import contextmanager
import logging
import threading
import time


class _ProgressHandler(logging.StreamHandler):
    def __init__(self):
        super().__init__()
        self.started = self.last_update = self.last_report = time.monotonic()
        self.last_message = "Starting download"

    def format(self, record):
        elapsed = int(time.monotonic() - self.started)
        return f"[{elapsed // 60:02d}:{elapsed % 60:02d}] {record.getMessage().strip()}"

    def emit(self, record):
        self.last_update = self.last_report = time.monotonic()
        self.last_message = record.getMessage().strip()
        super().emit(record)

    def report_if_idle(self, interval):
        # Serialize heartbeat output with service logs from other threads.
        with self.lock:
            now = time.monotonic()
            if now - self.last_report < interval:
                return
            idle = int(now - self.last_update)
            record = logging.LogRecord(
                "discogseek", logging.INFO, __file__, 0,
                "Still working (%ds since last update). Last update: %s",
                (idle, self.last_message), None,
            )
            super().emit(record)
            self.last_report = now


@contextmanager
def download_progress(quiet=False, interval=10.0):
    """Temporarily show package logs without changing audit/browser logging."""
    logger = logging.getLogger("discogseek")
    previous = logger.handlers[:], logger.level, logger.propagate
    handler = _ProgressHandler()
    logger.handlers = [handler]
    logger.setLevel(logging.WARNING if quiet else logging.INFO)
    logger.propagate = False
    stop = threading.Event()

    def heartbeat():
        while not stop.wait(interval):
            handler.report_if_idle(interval)

    worker = threading.Thread(target=heartbeat, name="download-progress", daemon=True)
    try:
        if not quiet:
            worker.start()
        yield
    finally:
        stop.set()
        if worker.ident is not None:
            worker.join()
        logger.handlers, level, logger.propagate = previous
        logger.setLevel(level)
        handler.close()
