//! Who owns an index file. Only one process may write one, and during a deploy the old and
//! new containers both run a server on it. The new server answers at once and queues the
//! writes it is sent, while its owner threads wait for the file's lock; the old server
//! applies what it has queued and exits on SIGTERM, which releases the lock to the new one.

use std::{collections::HashMap, path::PathBuf};

use anyhow::Context;
use mwmbl_core::indexer::{LockPolicy, TinyIndex};

use crate::api::Indexes;

/// Opens every index for writing without waiting for its lock: requests queue until the
/// owner threads have taken it.
///
/// # Errors
///
/// Fails if an index can't be opened.
pub fn open_indexes(
    index_paths: &[(String, PathBuf)],
    num_threads: usize,
) -> anyhow::Result<HashMap<String, TinyIndex>> {
    index_paths
        .iter()
        .map(|(name, path)| {
            let index = TinyIndex::open_writer(path, num_threads, LockPolicy::Wait)
                .with_context(|| format!("Could not open the {name} index"))?;
            Ok((name.clone(), index))
        })
        .collect()
}

/// Drops the indexes off the runtime. Dropping the last handle to an index joins its owner
/// threads, which first apply everything queued, and that would block a runtime thread.
///
/// # Errors
///
/// Fails if the blocking task can't be joined.
pub async fn close(indexes: Indexes) -> anyhow::Result<()> {
    tokio::task::spawn_blocking(move || drop(indexes))
        .await
        .context("Could not close the indexes")
}
