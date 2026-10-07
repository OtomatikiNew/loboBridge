"""JSON files under the add-on's /data directory.

Writes go to a temp file, get fsynced and are then renamed over the target,
so a power cut never leaves a half-written file.
"""

import json
import os
from typing import Any, Optional

DATA_DIR = "/data"


def write_json_atomic(path: str, data: Any) -> None:
    """Replaces `path` with `data` as JSON. Created with mode 0600 since
    callers store credentials.

    Args:
        path: Destination file.
        data: JSON-serializable value.

    Raises:
        OSError: If the write or rename fails. The old file is left as is.
    """
    tmp_path = f"{path}.tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_json(path: str) -> Optional[Any]:
    """Reads a JSON file.

    Args:
        path: File to read.

    Returns:
        The decoded value, or None if the file is missing or invalid.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def remove_file(path: str) -> None:
    """Deletes a file if it exists.

    Args:
        path: File to delete.
    """
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
