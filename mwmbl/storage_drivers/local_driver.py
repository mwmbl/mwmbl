from logging import getLogger
from pathlib import Path

from mwmbl.settings import STORAGE_BASE_PATH

logger = getLogger(__name__)


def _safe_build_path(base: Path, suffix: str):
    base = base.resolve()
    combined = (base / suffix).resolve()
    if not combined.is_relative_to(base):
        raise PermissionError("Unsafe path traversal.")
    return combined


def upload(data: bytes, name: str):
    logger.info(f"Writing {len(data)} bytes to {name}")
    path = _safe_build_path(Path(STORAGE_BASE_PATH), name)
    path.parent.mkdir(exist_ok=True, parents=True)
    with open(path, "wb") as file:
        file.write(data)


def get_batches_for_prefix(prefix: str):
    path = _safe_build_path(Path(STORAGE_BASE_PATH), prefix)
    if not path.exists():
        return []
    glob = path.glob("**/*")
    return [str(x.relative_to(STORAGE_BASE_PATH)) for x in glob if x.is_file()]


def get_subfolders(prefix: str):
    path = _safe_build_path(Path(STORAGE_BASE_PATH), prefix)
    if not path.exists():
        return []
    dir_elems = path.iterdir()
    return [x.name for x in dir_elems if x.is_dir()]
