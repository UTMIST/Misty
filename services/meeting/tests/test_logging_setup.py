import io
import logging
import subprocess
import sys
from pathlib import Path

from src.logging_setup import configure_logging


def _reset(name: str = "src") -> logging.Logger:
    logger = logging.getLogger(name)
    for h in list(logger.handlers):
        logger.removeHandler(h)
    logger.setLevel(logging.NOTSET)
    return logger


def test_an_info_record_from_a_service_module_actually_gets_out():
    """The bug this exists for: uvicorn configures only its own `uvicorn*`
    loggers and leaves ROOT with no handler, so a module logger falls through to
    `logging.lastResort` -- which emits WARNING and above and drops the rest.
    The startup note went out at INFO and never appeared in Railway's logs once.
    """
    _reset()
    try:
        configure_logging()
        logger = logging.getLogger("src.api.app")
        assert logger.getEffectiveLevel() == logging.INFO, (
            "an INFO record from this service is still below the cutoff"
        )

        buffer = io.StringIO()
        handler = logging.getLogger("src").handlers[0]
        original, handler.stream = handler.stream, buffer
        try:
            logger.info("the-note")
        finally:
            handler.stream = original

        assert "the-note" in buffer.getvalue()
    finally:
        _reset()


def test_configuring_twice_does_not_double_the_output():
    """create_app() runs once per test in this suite. Topping up a handler each
    time would duplicate every line, which reads as a retry loop in the logs."""
    _reset()
    try:
        configure_logging()
        configure_logging()
        configure_logging()

        assert len(logging.getLogger("src").handlers) == 1
    finally:
        _reset()


def test_records_still_propagate_so_caplog_keeps_working(caplog):
    """`propagate = False` would have been the tidier-looking fix and would have
    blinded every existing test that asserts on log output."""
    _reset()
    try:
        configure_logging()
        with caplog.at_level(logging.WARNING):
            logging.getLogger("src.sessions").warning("still-visible")

        assert "still-visible" in caplog.text
    finally:
        _reset()


def test_third_party_loggers_are_left_alone():
    """Switching ROOT to INFO instead would have turned on botocore and httpx
    chatter for the sake of one startup line."""
    _reset()
    try:
        configure_logging()

        assert logging.getLogger("botocore").getEffectiveLevel() > logging.INFO
        # pytest installs handlers of its own on root, so check for OURS rather
        # than for an empty list.
        ours = [
            h for h in logging.getLogger().handlers if getattr(h, "_meeting_service_handler", False)
        ]
        assert not ours, "the service handler was attached to root"
    finally:
        _reset()


def test_the_startup_note_reaches_stdout_under_real_uvicorn_logging():
    """End-to-end, in a FRESH interpreter with uvicorn's real LOGGING_CONFIG
    applied exactly as `uvicorn src.api.app:app` applies it. The in-process
    tests above all run with pytest's logging already installed, so only this
    one reproduces the deployed condition that actually failed.
    """
    script = (
        "import logging.config\n"
        "from uvicorn.config import LOGGING_CONFIG\n"
        "logging.config.dictConfig(LOGGING_CONFIG)\n"
        "import src.api.app\n"  # runs create_app() at import, as uvicorn does
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "process memory" in result.stdout, (
        "the single-instance note is still invisible under uvicorn:\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
