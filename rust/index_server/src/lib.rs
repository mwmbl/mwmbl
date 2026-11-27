#![cfg_attr(
    not(test),
    deny(
        clippy::exit,
        clippy::expect_used,
        clippy::indexing_slicing,
        clippy::unwrap_used,
        clippy::panic,
        clippy::string_slice,
        clippy::unimplemented,
        clippy::unreachable,
        clippy::todo,
        clippy::pedantic
    )
)]
//! The index server: the only writer to the server's index files. Clients send it
//! operations over HTTP on a Unix socket, and each index's owner threads apply them, so no
//! client ever does a read-modify-write of a page itself.

pub mod api;
pub mod ownership;

use std::{
    fs,
    path::{Path, PathBuf},
    sync::Arc,
};

use anyhow::Context;
use tokio::{
    net::UnixListener,
    signal::unix::{SignalKind, signal},
};

/// Serves the indexes at `index_paths`, named by their keys, on `socket_path` until
/// `shutdown` completes, and then until every request already queued has been applied.
///
/// # Errors
///
/// Fails if an index can't be opened, the socket can't be bound, or serving fails.
pub async fn serve(
    socket_path: &Path,
    index_paths: &[(String, PathBuf)],
    num_threads: usize,
    shutdown: impl Future<Output = ()> + Send + 'static,
) -> anyhow::Result<()> {
    let indexes = Arc::new(ownership::open_indexes(index_paths, num_threads)?);
    // A socket left by a server that this container ran before, which nothing listens on.
    if socket_path.exists() {
        fs::remove_file(socket_path).context("Could not remove the old index server socket")?;
    }
    let listener = UnixListener::bind(socket_path).context("Could not bind the socket")?;
    axum::serve(listener, api::router(Arc::clone(&indexes)))
        .with_graceful_shutdown(shutdown)
        .await
        .context("The index server failed")?;
    ownership::close(indexes).await
}

/// Runs `serve` on a runtime of its own until the process gets SIGTERM.
///
/// # Errors
///
/// Fails as `serve` does, or if the runtime or the signal handler can't be set up.
pub fn run(
    socket_path: &Path,
    index_paths: &[(String, PathBuf)],
    num_threads: usize,
) -> anyhow::Result<()> {
    let runtime = tokio::runtime::Runtime::new().context("Could not start a runtime")?;
    runtime.block_on(async {
        let mut terminate = signal(SignalKind::terminate()).context("Could not handle SIGTERM")?;
        let shutdown = async move {
            terminate.recv().await;
        };
        serve(socket_path, index_paths, num_threads, shutdown).await
    })
}
