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
pub mod blacklist;
pub mod blacklist_providers;
pub mod constants;
pub mod hn_top_domains_filtered;
pub mod index;
pub mod indexer;
