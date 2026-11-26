//! Turns `wiki_stats.json` into a sorted, read-only table that `wiki.rs` includes.
//!
//! As a `HashMap` built at runtime the 500k page view counts cost each process ~60MB of heap.
//! Embedded, they live in the binary's read-only data instead.
//!
//! It writes the titles, sorted bytewise and one per line, as `wiki_titles.txt`, and a Rust
//! file (`wiki_table.rs`) holding each title's score, in the same order: its count divided by
//! the largest. A generated `[&str; N]` would cost a relocation per title when the library
//! loads, and makes the build ~15 s slower and the binary twice the size.

use std::collections::HashMap;
use std::env;
use std::fmt::Write;
use std::fs;
use std::path::PathBuf;

const WIKI_STATS: &str = "../../mwmbl/resources/wiki_stats.json";

fn main() {
    println!("cargo:rerun-if-changed={WIKI_STATS}");
    println!("cargo:rerun-if-changed=build.rs");

    let json = fs::read_to_string(WIKI_STATS).expect("Failed to read wiki_stats.json");
    let counts: HashMap<String, f64> =
        serde_json::from_str(&json).expect("Failed to parse wiki_stats.json");
    let max = counts.values().copied().fold(0.0, f64::max);
    assert!(max > 0.0, "wiki_stats.json has no positive counts");

    let mut entries: Vec<(&String, f64)> = counts.iter().map(|(k, v)| (k, v / max)).collect();
    entries.sort_unstable_by(|a, b| a.0.cmp(b.0));

    let titles: Vec<&str> = entries.iter().map(|(title, _)| title.as_str()).collect();
    assert!(
        titles.iter().all(|title| !title.contains('\n')),
        "a wiki title contains a newline, which wiki_titles.txt separates them with"
    );
    let mut scores = String::new();
    for (_, score) in &entries {
        // Debug prints the shortest literal that reads back as the same f64.
        write!(scores, "{score:?},").unwrap();
    }
    let num_titles = entries.len();
    let table = format!(
        "static TITLE_LINES: &str = include_str!(concat!(env!(\"OUT_DIR\"), \"/wiki_titles.txt\"));\n\
         #[allow(clippy::unreadable_literal)]\n\
         static SCORES: [f64; {num_titles}] = [{scores}];\n"
    );

    let out_dir = PathBuf::from(env::var("OUT_DIR").unwrap());
    fs::write(out_dir.join("wiki_titles.txt"), titles.join("\n"))
        .expect("Failed to write wiki_titles.txt");
    fs::write(out_dir.join("wiki_table.rs"), table).expect("Failed to write wiki_table.rs");
}
