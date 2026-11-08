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
pub mod index;
pub mod indexer;
pub mod constants;
pub mod blacklist_providers;
pub mod curated_domains;

