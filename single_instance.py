"""Per-user OS file lock, released automatically when the process exits."""
import hashlib
import os
from pathlib import Path
from dictionary_manager import user_data_dir


class SingleInstanceGuard:
    def __init__(self, name="offline-translator"):
        folder = Path(user_data_dir())
        folder.mkdir(parents=True, exist_ok=True)
        identifier = hashlib.sha256(name.encode()).hexdigest()[:16]
        self._file = open(folder / (identifier + ".lock"), "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self._file.seek(0)
                if not self._file.read(1):
                    self._file.write(b"0")
                    self._file.flush()
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._file.close()
            self._file = None

    @property
    def acquired(self):
        return self._file is not None

    def close(self):
        if self._file is not None:
            self._file.close()
            self._file = None
