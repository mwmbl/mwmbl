use std::path::{Path, PathBuf};

use crate::constants::{DATA_DIR, INDEX_NAME};

fn data_path(home: &Path) -> PathBuf {
    let mut path = home.to_path_buf();
    path.push(DATA_DIR);
    path
}

pub fn index_path(home: &Path) -> PathBuf {
    let mut path = data_path(home);
    path.push(INDEX_NAME);
    path
}
