"""Local, redacted, rotating logs. No telemetry (docs/SECURITY.md, threat 19)."""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

from goulot.security.fs import FILE_MODE, ensure_private_dir, touch_private
from goulot.security.redact import RedactingFormatter

LOG_FILE = "goulot.log"
MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5
_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class _PrivateRotatingHandler(logging.handlers.RotatingFileHandler):
    def _open(self):  # type: ignore[no-untyped-def]
        stream = super()._open()
        os.chmod(self.baseFilename, FILE_MODE)
        return stream

    def doRollover(self) -> None:
        super().doRollover()
        for i in range(1, self.backupCount + 1):
            rotated = Path(f"{self.baseFilename}.{i}")
            if rotated.exists():
                os.chmod(rotated, FILE_MODE)


def setup_logging(log_dir: Path, *, console: bool = False, level: int = logging.INFO) -> Path:
    ensure_private_dir(log_dir)
    path = log_dir / LOG_FILE
    touch_private(path)
    handler = _PrivateRotatingHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
    handler.setFormatter(RedactingFormatter(_FORMAT))
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(RedactingFormatter("%(levelname)s %(message)s"))
        root.addHandler(stream)
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "apscheduler"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    logging.getLogger("uvicorn.access").disabled = True  # our middleware logs method, path and status only
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    logging.getLogger("httpcore2").setLevel(logging.WARNING)
    return path
