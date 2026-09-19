"""Visible idle feedback and cleanup on every command exit."""

import logging
import threading

import pytest

from discogseek.cli import progress


def test_idle_heartbeat_tracks_elapsed_time_and_last_update(monkeypatch, capsys):
    now = [100.0]
    monkeypatch.setattr(progress.time, "monotonic", lambda: now[0])
    handler = progress._ProgressHandler()
    record = logging.LogRecord("discogseek", logging.INFO, __file__, 0, "Loading catalog...", (), None)
    handler.handle(record)
    assert capsys.readouterr().err == "[00:00] Loading catalog...\n"

    now[0] = 109.0
    handler.report_if_idle(10)
    assert capsys.readouterr().err == ""
    now[0] = 110.0
    handler.report_if_idle(10)
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "[00:10] Still working (10s since last update). Last update: Loading catalog...\n"
    now[0] = 120.0
    handler.report_if_idle(10)
    assert "20s since last update" in capsys.readouterr().err

    record.msg = "Searching Soulseek..."
    handler.handle(record)
    capsys.readouterr()
    now[0] = 129.0
    handler.report_if_idle(10)
    assert capsys.readouterr().err == ""
    now[0] = 130.0
    handler.report_if_idle(10)
    assert "10s since last update). Last update: Searching Soulseek..." in capsys.readouterr().err
    handler.close()


@pytest.mark.parametrize("error", [None, RuntimeError, KeyboardInterrupt])
def test_progress_stops_and_restores_logging_on_exit(error):
    logger = logging.getLogger("discogseek")
    previous = logger.handlers[:], logger.level, logger.propagate
    worker = None
    try:
        with progress.download_progress():
            worker = next(t for t in threading.enumerate() if t.name == "download-progress")
            assert worker.is_alive()
            if error:
                raise error()
    except (RuntimeError, KeyboardInterrupt):
        if error is None:
            raise
    assert worker is not None and not worker.is_alive()
    assert (logger.handlers, logger.level, logger.propagate) == previous


def test_quiet_progress_keeps_warnings_without_starting_heartbeat(capsys):
    with progress.download_progress(quiet=True):
        logging.getLogger("discogseek.test").info("Hidden progress")
        logging.getLogger("discogseek.test").warning("Visible warning")
        assert not any(t.name == "download-progress" for t in threading.enumerate())
    output = capsys.readouterr()
    assert "Hidden progress" not in output.err
    assert "Visible warning" in output.err
    assert output.out == ""
