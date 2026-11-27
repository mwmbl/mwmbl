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
    io::ErrorKind,
    os::unix::fs::{FileTypeExt, PermissionsExt},
    path::{Path, PathBuf},
    sync::Arc,
};

use anyhow::{Context, bail};
use tokio::{
    net::UnixListener,
    signal::unix::{SignalKind, signal},
};

/// Enough runtime threads to parse and answer requests; the index work is done on the
/// indexes' own owner threads.
const WORKER_THREADS: usize = 4;

/// Serves the indexes at `index_paths`, named by their keys, on `socket_path` until
/// `shutdown` completes, and then until every request already queued has been applied. An
/// index whose lock isn't held by then, because the server it replaces never released it,
/// fails the requests queued for it rather than waiting.
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
    let listener = bind(socket_path)?;
    let waiting = Arc::clone(&indexes);
    let shutdown = async move {
        shutdown.await;
        for index in waiting.values() {
            index.abandon_lock_wait();
        }
    };
    axum::serve(listener, api::router(Arc::clone(&indexes)))
        .with_graceful_shutdown(shutdown)
        .await
        .context("The index server failed")?;
    ownership::close(indexes).await
}

/// Binds the socket, readable and writable by this user alone. A socket already at the
/// path is one a server this container ran before left, which nothing listens on.
fn bind(socket_path: &Path) -> anyhow::Result<UnixListener> {
    match fs::symlink_metadata(socket_path) {
        Ok(metadata) if metadata.file_type().is_socket() => {
            fs::remove_file(socket_path).context("Could not remove the old index server socket")?;
        }
        Ok(_) => bail!("{} exists and is not a socket", socket_path.display()),
        Err(error) if error.kind() == ErrorKind::NotFound => {}
        Err(error) => return Err(error).context("Could not check the socket path"),
    }
    let listener = UnixListener::bind(socket_path).context("Could not bind the socket")?;
    fs::set_permissions(socket_path, fs::Permissions::from_mode(0o600))
        .context("Could not restrict the socket")?;
    Ok(listener)
}

/// Runs `serve` on a runtime of its own until the process gets SIGTERM or SIGINT.
///
/// # Errors
///
/// Fails as `serve` does, or if the runtime or the signal handlers can't be set up.
pub fn run(
    socket_path: &Path,
    index_paths: &[(String, PathBuf)],
    num_threads: usize,
) -> anyhow::Result<()> {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(WORKER_THREADS)
        .enable_all()
        .build()
        .context("Could not start a runtime")?;
    runtime.block_on(async {
        let mut terminate = signal(SignalKind::terminate()).context("Could not handle SIGTERM")?;
        let mut interrupt = signal(SignalKind::interrupt()).context("Could not handle SIGINT")?;
        let shutdown = async move {
            tokio::select! {
                _ = terminate.recv() => {}
                _ = interrupt.recv() => {}
            }
        };
        serve(socket_path, index_paths, num_threads, shutdown).await
    })
}
