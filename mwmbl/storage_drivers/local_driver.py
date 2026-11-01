from logging import getLogger
from pathlib import Path

logger = getLogger(__name__)

from mwmbl.settings import STORAGE_BASE_PATH

def upload(data: bytes, name: str):
    logger.info(f"Writing {len(data)} bytes to {name}")
    path = Path(STORAGE_BASE_PATH)/name
    path.parent.mkdir(exist_ok=True, parents=True)
    with open(path, 'wb') as file:
        file.write(data)

def get_batches_for_prefix(prefix: str):
    path = Path(STORAGE_BASE_PATH)/prefix
    if not path.exists():
        return []
    glob = path.glob('**/*')
    return [str(x) for x in glob if x.is_file()]

def get_subfolders(prefix: str):
    path = Path(STORAGE_BASE_PATH)/prefix
    if not path.exists():
        return []
    dir_elems = path.iterdir()
    return [x.name for x in dir_elems if x.is_dir()]
