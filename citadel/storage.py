from pathlib import Path

from config import get_settings


def _object_path(key: str) -> Path:
    base = get_settings().tree_store_dir.resolve()
    path = (base / key).resolve()
    if not path.is_relative_to(base):
        msg = f"object key escapes store: {key}"
        raise ValueError(msg)
    return path


def put_object(key: str, data: bytes) -> None:
    path = _object_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def get_object(key: str) -> bytes | None:
    path = _object_path(key)
    if not path.exists():
        return None
    return path.read_bytes()


def delete_object(key: str) -> None:
    _object_path(key).unlink(missing_ok=True)
