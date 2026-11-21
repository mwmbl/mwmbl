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
mod batch;
mod bloomdb;
mod constants;
mod crawler;
mod curated_domains;
mod path_creation;
mod retrieve;
mod robots;
mod url_queue;

use std::path::PathBuf;

use anyhow::Ok;
use clap::{Args, Parser, ValueEnum};
use mwmbl_core::indexer::create_index;

use crate::{constants::NUM_PAGES, crawler::Crawler, path_creation::index_path};

#[derive(Debug, Parser)] // requires `derive` feature
#[command(name = "crawler")]
#[command(bin_name = "crawler")]
#[command(version, about = "mwmbl crawler, rust implementation", long_about = None)]
struct Cli {
    #[command(flatten)]
    crawler_params: CrawlerParams,
    /// number of async workers
    #[arg(env = "CRAWL_THREADS", long, default_value = "32")]
    crawler_workers: usize,
    /// number of blocking workers (mainly used for html parsing)
    #[arg(env = "PARSING_THREADS", long, default_value = "8")]
    parsing_workers: usize,
}

#[derive(Debug, Clone, Args)]
pub struct CrawlerParams {
    /// API key for mwmbl server
    #[arg(env = "MWMBL_API_KEY", long, required = true)]
    api_key: String,
    /// Contact information configuration - required for responsible crawling
    #[arg(env = "MWMBL_CONTACT_INFO", long, required = true)]
    contact_info: String,
    /// Name this crawler reports itself as, so its owner can tell their crawlers apart in the
    /// devices list. Left unset, a random id is generated on first run and kept in the data
    /// directory - see mwmbl.crawler.device. The machine hostname is deliberately never used.
    #[arg(env = "MWMBL_DEVICE_NAME", long, required = false)]
    device_name: Option<String>,
    /// Whether to send the errors the crawl loops catch to the Mwmbl server, so we can see when
    /// crawlers are failing. Reports carry the exception and traceback, the crawler version and
    /// the device name. File paths are sent relative to the Python path, and the home directory
    /// is replaced with ~ in the message, so a report does not name the volunteer's account.
    #[arg(env = "CRAWL_REPORT_ERRORS", long, default_value = "true")]
    crawl_report_errors: bool,
    /// Whether to send the errors the crawl loops catch to the Mwmbl server, so we can see when
    /// crawlers are failing. Reports carry the exception and traceback, the crawler version and
    /// the device name. File paths are sent relative to the Python path, and the home directory
    /// is replaced with ~ in the message, so a report does not name the volunteer's account.
    #[arg(env = "CRAWL_DELAY_SECONDS", long, default_value = "0.0")]
    crawl_delay_seconds: f32,
    /// What to do with results the crawler decides are worth submitting:
    #[arg(env = "CRAWL_SUBMIT_MODE", long, value_enum, default_value = "index")]
    submit_mode: CrawlSubmitMode,
    /// Where the crawler reads the main index from and submits its results to.
    #[arg(
        env = "MWMBL_REMOTE_SERVER",
        long,
        default_value = "https://api.mwmbl.org"
    )]
    remote_server: String,
    /// maximum amount of simultaneous crawling task that can run at any point.
    #[arg(env = "CRAWL_MAX_TASKS", long, default_value = "64")]
    max_crawl_task: usize,
    /// user home directory, used to derive data path
    #[arg(env = "HOME", long)]
    home: PathBuf,
}

#[derive(Debug, Clone, ValueEnum)]
enum CrawlSubmitMode {
    /// post them normally, so they go into the Mwmbl index (the default)
    Index,
    /// post them with ?`dry_run=true`, so the server authenticates and validates them but indexes nothing
    DryRun,
    /// do not post at all, and do not require an API key
    Off,
}

fn main() -> anyhow::Result<()> {
    let args = Cli::parse();
    println!("{:?}", args.crawler_params);

    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(args.crawler_workers)
        .enable_all()
        .max_blocking_threads(args.parsing_workers)
        .build()?
        .block_on(async move { tokio_main(args).await })
}

async fn tokio_main(args: Cli) -> anyhow::Result<()> {
    create_index(&index_path(&args.crawler_params.home), NUM_PAGES, false)?;

    let mut crawler = Crawler::new(args.crawler_params).await?;
    crawler.run().await?;
    Ok(())
}
