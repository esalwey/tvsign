"""Small file helpers shared by the backend."""
import os
import time
from pathlib import Path


def replace_retry(src: Path, dst: Path, attempts: int = 6, delay: float = 0.25) -> None:
    """os.replace with retries: on Windows an antivirus or indexer briefly holding the target
    makes replace fail with PermissionError."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay * (i + 1))


def write_private(path: Path, data: bytes | str) -> None:
    """Write a secret file readable by the owner only (0600 on POSIX), atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    raw = data.encode("utf-8") if isinstance(data, str) else data
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, raw)
    finally:
        os.close(fd)
    replace_retry(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
