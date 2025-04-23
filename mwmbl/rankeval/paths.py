import os
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent.parent
DATA_DIR = ROOT_DIR / 'devdata' / 'rankeval-2024-06'
QUERIES_DATASET_PATH = DATA_DIR / 'queries.csv'

REMOTE_DATA_DIR = DATA_DIR / 'remote-datasets'
RANKINGS_DATASET_TRAIN_PATH = REMOTE_DATA_DIR / 'rankings-train.csv'
RANKINGS_DATASET_TEST_PATH = REMOTE_DATA_DIR / 'rankings-test.csv'

LEARNING_TO_RANK_DATASET_PATH = DATA_DIR / 'learning-to-rank.csv.gz'
# MODEL_PATH = ROOT_DIR / "mwmbl" / "resources" / 'model.pickle'
MODEL_PATH = DATA_DIR / 'model.pickle'

URLS_PATH = Path(os.environ['HOME']) / 'data' / 'tinysearch' / 'urls.sqlite3'
