#![deny(
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
)]
#![allow(clippy::missing_errors_doc)]
#[cfg(feature = "crawler")]
pub mod blacklist;
#[cfg(feature = "crawler")]
pub mod blacklist_providers;
#[cfg(feature = "crawler")]
pub mod constants;
#[cfg(feature = "crawler")]
pub mod hn_top_domains_filtered;
pub mod index;
#[cfg(feature = "crawler")]
pub mod indexer;
