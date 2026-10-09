from pathlib import Path
import hashlib
import json
import os


def configuration_files(path):
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    paths = [path]
    if data.get('profile'):
        paths.append((path.parent / data['profile']).resolve())
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def configuration_digest(path):
    return hashlib.sha256(json.dumps(configuration_files(path), sort_keys=True).encode()).hexdigest()


class ProcessLock:
    def __init__(self, path):
        self.path = Path(path)

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            self.file.close()
            raise RuntimeError("another program is operating this project; stop that run first") from e
        return self

    def __exit__(self, *_):
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()
