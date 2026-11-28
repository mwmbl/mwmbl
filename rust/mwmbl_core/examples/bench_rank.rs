//! Checks the ranker against the regex version on documents from a real index, then times
//! indexing batches of new documents into it.
//!
//! Usage: `cargo run --release --example bench_rank -- <index file> <batches>`. Indexing
//! writes to the file, so use a copy.
use std::fs::File;
use std::path::Path;
use std::time::{Duration, Instant};

use mwmbl_core::blacklist_providers::BuiltInRulesBlacklistProvider;
use mwmbl_core::index::read_index_page;
use mwmbl_core::indexer::{Document, IndexMode, TinyIndex};
use mwmbl_core::rank::{QueryMatchers, get_features_regex, get_features_with, get_query_regex};

const PAGE_SIZE: u64 = 4096;
const HEADER_SIZE: u64 = 4096;

fn main() -> anyhow::Result<()> {
    let mut args = std::env::args().skip(1);
    let path = args.next().unwrap_or_default();
    let batches: usize = args.next().unwrap_or_default().parse()?;

    let file = File::open(&path)?;
    let mut pages = Vec::new();
    for page in 0..100_000u64 {
        let offset = HEADER_SIZE + page * PAGE_SIZE;
        if let Ok(docs) = read_index_page::<Document>(&file, offset, 4096, None) {
            pages.push(docs);
        }
    }
    check_features_match_regex(&pages)?;

    let samples: Vec<&Document> = pages
        .iter()
        .step_by(37)
        .filter_map(|docs| docs.first())
        .collect();
    let index = TinyIndex::from_file(Path::new(&path), IndexMode::WRITE, 4)?;
    let blacklist = BuiltInRulesBlacklistProvider::new();
    let runtime = tokio::runtime::Builder::new_current_thread().build()?;
    let mut total = Duration::ZERO;
    for batch in 0..batches {
        let docs: Vec<Document> = (0..100)
            .map(|i| {
                let n = batch * 100 + i;
                let sample = samples[n % samples.len()];
                Document::new(
                    sample.title().to_owned(),
                    format!("https://bench{n}.example.org/page/{n}"),
                    sample.extract().to_owned(),
                    None,
                    None,
                    None,
                    None,
                    Some(1_700_000_000),
                    None,
                )
            })
            .collect();
        let start = Instant::now();
        runtime.block_on(index.index_documents(docs, &blacklist))?;
        total += start.elapsed();
    }
    println!(
        "{batches} batches of 100 in {total:.2?}: {:.1} ms/batch",
        total.as_secs_f64() * 1e3 / batches as f64
    );
    Ok(())
}

/// Every stored document's features for its own term, scored both ways, must be identical.
fn check_features_match_regex(pages: &[Vec<Document>]) -> anyhow::Result<()> {
    let mut checked = 0;
    let mut non_ascii_terms = 0;
    for doc in pages.iter().flatten() {
        let Some(term) = doc.term() else { continue };
        let terms: Vec<&str> = term.split_whitespace().collect();
        let expected = get_features_regex(
            &terms,
            doc,
            &get_query_regex(&terms, true, false)?,
            &get_query_regex(&terms, true, true)?,
        );
        let actual = get_features_with(&QueryMatchers::new(&terms, true), doc);
        match (expected, actual) {
            (Ok(expected), Ok(actual)) => {
                assert_eq!(expected, actual, "{term:?} {}", doc.url());
            }
            (Err(_), Err(_)) => {}
            (expected, actual) => panic!("{term:?} {}: {expected:?} vs {actual:?}", doc.url()),
        }
        checked += 1;
        non_ascii_terms += usize::from(!term.is_ascii());
    }
    println!(
        "{checked} documents scored the same as the regex ({non_ascii_terms} non-ASCII terms)"
    );
    Ok(())
}
