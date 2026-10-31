//! Turns wiki_stats.json into a sorted, read-only table that wiki.rs embeds with include_bytes!.
//!
//! As a HashMap built at runtime the 500k page view counts cost each process ~60MB of heap.
//! Embedded, they live in the shared library's read-only data: mapped from the file, shared by
//! every process that loads it, and paged out under memory pressure rather than swapped.
//!
//! Layout, all little-endian: n: u32, then n + 1 key offsets: u32, then n scores: f64 (each
//! count divided by the largest), then the keys' UTF-8 bytes, sorted bytewise.

use std::collections::HashMap;
use std::env;
use std::fs;
use std::path::PathBuf;

const WIKI_STATS: &str = "../mwmbl/resources/wiki_stats.json";

fn main() {
    println!("cargo:rerun-if-changed={WIKI_STATS}");
    println!("cargo:rerun-if-changed=build.rs");

    let json = fs::read_to_string(WIKI_STATS).expect("Failed to read wiki_stats.json");
    let counts: HashMap<String, f64> = serde_json::from_str(&json).expect("Failed to parse wiki_stats.json");
    let max = counts.values().cloned().fold(0.0, f64::max);

    let mut entries: Vec<(&String, f64)> = counts.iter().map(|(k, v)| (k, if max > 0.0 { v / max } else { 0.0 })).collect();
    entries.sort_unstable_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));

    let n = entries.len() as u32;
    let mut out = Vec::new();
    out.extend_from_slice(&n.to_le_bytes());
    let mut offset = 0u32;
    out.extend_from_slice(&offset.to_le_bytes());
    for (key, _) in &entries {
        offset += key.len() as u32;
        out.extend_from_slice(&offset.to_le_bytes());
    }
    for (_, score) in &entries {
        out.extend_from_slice(&score.to_le_bytes());
    }
    for (key, _) in &entries {
        out.extend_from_slice(key.as_bytes());
    }

    let path = PathBuf::from(env::var("OUT_DIR").unwrap()).join("wiki_scores.bin");
    fs::write(path, out).expect("Failed to write wiki_scores.bin");
}
