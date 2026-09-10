"""Advisory single-writer lock, automatically released if the process exits."""

import sys
from pathlib import Path


class WorkspaceLock:
    def __init__(self, path: Path) -> None:
        self.stream = path.open("a+b")
        self.stream.seek(0, 2)
        if self.stream.tell() == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.stream.close()
            raise ValueError(
                "This results directory is already open in another Chess Lab process"
            ) from error

    def close(self) -> None:
        self.stream.close()
