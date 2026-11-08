#![deny(
    clippy::arithmetic_side_effects,
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
mod crawler;
mod redis_url_queue;
mod constants;


use std::path::{Path, PathBuf};

use clap::{Args, Parser, ValueEnum};
use mwmbl_core::indexer::{IndexMode, TinyIndex, create_index};

use crate::{constants::{DATA_DIR, INDEX_NAME, NUM_PAGES}, crawler::Crawler};

#[derive(Debug, Parser)] // requires `derive` feature
#[command(name = "crawler")]
#[command(bin_name = "crawler")]
#[command(version, about = "mwmbl crawler, rust implementation", long_about = None)]
struct Cli {
    /// Crawl one batch, index it and exit, failing on the first error instead of 
    /// restarting. Intended for smoke tests, not for crawling.
    #[arg(long)]
    once: bool,
    #[command(flatten)]
    crawler_params: CrawlerParams,
}

#[derive(Debug, Clone, Args)]
pub struct CrawlerParams {
    /// number of workers
    #[arg(env = "CRAWLER_WORKERS", long, default_value="2")]
    crawler_workers: i32,
    /// number of threads
    #[arg(env = "CRAWLER_WORKERS", long, default_value="20")]
    crawl_threads: i32,
    /// API key for mwmbl server
    #[arg(env = "MWMBL_API_KEY", long, required=true)]
    api_key: String,
    /// Contact information configuration - required for responsible crawling
    #[arg(env = "MWMBL_CONTACT_INFO", long, required=true)]
    contact_info: String,
    /// Name this crawler reports itself as, so its owner can tell their crawlers apart in the
    /// devices list. Left unset, a random id is generated on first run and kept in the data
    /// directory - see mwmbl.crawler.device. The machine hostname is deliberately never used.
    #[arg(env = "MWMBL_DEVICE_NAME", long, required=false)]
    device_name: Option<String>,
    /// Whether to send the errors the crawl loops catch to the Mwmbl server, so we can see when
    /// crawlers are failing. Reports carry the exception and traceback, the crawler version and
    /// the device name. File paths are sent relative to the Python path, and the home directory
    /// is replaced with ~ in the message, so a report does not name the volunteer's account.
    #[arg(env = "CRAWL_REPORT_ERRORS", long, default_value="true")]
    crawl_report_errors: bool,
    /// URLs assigned per batch.
    #[arg(env = "CRAWL_BATCH_SIZE", long, default_value="100")]
    crawl_batch_size: i32,
    /// URLs assigned per batch.
    #[arg(env = "CRAWL_ALLOWED_DOMAINS", long, required=false, value_delimiter = ',')]
    crawl_allowed_domains: Option<Vec<String>>,
    /// What to do with results the crawler decides are worth submitting:
    #[arg(env = "CRAWL_SUBMIT_MODE", long, value_enum, default_value="index")]
    submit_mode: CrawlSubmitMode,
    /// Where the crawler reads the main index from and submits its results to.
    #[arg(env = "MWMBL_REMOTE_SERVER", long, default_value="https://api.mwmbl.org")]
    remote_server: String,
    /// user home directory, used to derive data path
    #[arg(env = "HOME", long)]
    home: PathBuf,
    /// Redis configuration url
    #[arg(env = "REDIS_URL", long, default_value="redis://127.0.0.1:6379")]
    redis_url: String,
}

#[derive(Debug, Clone, ValueEnum)]
enum CrawlSubmitMode {
    /// post them normally, so they go into the Mwmbl index (the default)
    Index,
    /// post them with ?`dry_run=true`, so the server authenticates and validates them but indexes nothing
    DryRun,
    /// do not post at all, and do not require an API key
    Off
}

fn main() -> anyhow::Result<()> {
    let args = Cli::parse();
    println!("{:?}", args.crawler_params);

    let mut index_path = args.crawler_params.home.clone();
    index_path.push(DATA_DIR);
    index_path.push(INDEX_NAME);

    create_index(&index_path, NUM_PAGES, false)?;
    
    let crawler = Crawler::new(args.crawler_params)?;
    if args.once {
        crawler.run_once()
    } else {
        crawler.run()
    }
}

fn run() -> anyhow::Result<()> {
   todo!();
}

fn run_once() -> anyhow::Result<()> {
   todo!();
}