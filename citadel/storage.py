from pathlib import Path

from config import get_settings


def _object_path(key: str) -> Path:
    return get_settings().tree_store_dir / key


def put_object(key: str, data: bytes) -> None:
    path = _object_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def get_object(key: str) -> bytes | None:
    path = _object_path(key)
    if not path.exists():
        return None
    return path.read_bytes()
