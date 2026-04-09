/// TF-IDF feature computation.
/// Replicates get_tf_idf_features from mwmbl/tinysearchengine/rank.py.
/// Note: TF-IDF features are currently commented out in the Python code but kept here
/// for completeness and future use.

use std::collections::HashMap;
use once_cell::sync::Lazy;
use serde_json;

static DOCUMENT_COUNTS_JSON: &str = include_str!("../../mwmbl/resources/document_counts.json");

static DOCUMENT_FREQUENCIES: Lazy<HashMap<String, f64>> = Lazy::new(|| {
    serde_json::from_str(DOCUMENT_COUNTS_JSON).expect("Failed to parse document_counts.json")
});

static N_DOCUMENTS: Lazy<f64> = Lazy::new(|| {
    DOCUMENT_FREQUENCIES.values().cloned().fold(f64::NEG_INFINITY, f64::max)
});

/// Get the inverse document frequency for a term.
pub fn get_idf(term: &str) -> f64 {
    let df = DOCUMENT_FREQUENCIES.get(term).cloned().unwrap_or(1.0);
    (*N_DOCUMENTS / df).ln()
}

/// Compute TF-IDF statistics from a map of term -> count.
/// Returns a fixed set of statistics: max, min, mean, std, sum for tf_idf, tf, and idf.
#[derive(Debug, Clone)]
pub struct TfIdfFeatures {
    pub max_tf_idf: f64,
    pub min_tf_idf: f64,
    pub mean_tf_idf: f64,
    pub std_tf_idf: f64,
    pub sum_tf_idf: f64,
    pub max_tf: f64,
    pub min_tf: f64,
    pub mean_tf: f64,
    pub std_tf: f64,
    pub sum_tf: f64,
    pub max_idf: f64,
    pub min_idf: f64,
    pub mean_idf: f64,
    pub std_idf: f64,
    pub sum_idf: f64,
}

impl Default for TfIdfFeatures {
    fn default() -> Self {
        TfIdfFeatures {
            max_tf_idf: 0.0,
            min_tf_idf: 0.0,
            mean_tf_idf: 0.0,
            std_tf_idf: 0.0,
            sum_tf_idf: 0.0,
            max_tf: 0.0,
            min_tf: 0.0,
            mean_tf: 0.0,
            std_tf: 0.0,
            sum_tf: 0.0,
            max_idf: 0.0,
            min_idf: 0.0,
            mean_idf: 0.0,
            std_idf: 0.0,
            sum_idf: 0.0,
        }
    }
}

fn std_dev(values: &[f64], mean: f64) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    let variance = values.iter().map(|v| (v - mean).powi(2)).sum::<f64>() / values.len() as f64;
    variance.sqrt()
}

pub fn get_tf_idf_features(match_counts: &HashMap<String, usize>) -> TfIdfFeatures {
    if match_counts.is_empty() {
        return TfIdfFeatures::default();
    }

    let idfs: Vec<f64> = match_counts.keys().map(|term| get_idf(term)).collect();
    let tfs: Vec<f64> = match_counts.values().map(|&c| c as f64).collect();
    let tf_idfs: Vec<f64> = tfs.iter().zip(idfs.iter()).map(|(tf, idf)| tf * idf).collect();

    let sum_tf: f64 = tfs.iter().sum();
    let sum_idf: f64 = idfs.iter().sum();
    let sum_tf_idf: f64 = tf_idfs.iter().sum();
    let n = tfs.len() as f64;

    let mean_tf = sum_tf / n;
    let mean_idf = sum_idf / n;
    let mean_tf_idf = sum_tf_idf / n;

    TfIdfFeatures {
        max_tf_idf: tf_idfs.iter().cloned().fold(f64::NEG_INFINITY, f64::max),
        min_tf_idf: tf_idfs.iter().cloned().fold(f64::INFINITY, f64::min),
        mean_tf_idf,
        std_tf_idf: std_dev(&tf_idfs, mean_tf_idf),
        sum_tf_idf,
        max_tf: tfs.iter().cloned().fold(f64::NEG_INFINITY, f64::max),
        min_tf: tfs.iter().cloned().fold(f64::INFINITY, f64::min),
        mean_tf,
        std_tf: std_dev(&tfs, mean_tf),
        sum_tf,
        max_idf: idfs.iter().cloned().fold(f64::NEG_INFINITY, f64::max),
        min_idf: idfs.iter().cloned().fold(f64::INFINITY, f64::min),
        mean_idf,
        std_idf: std_dev(&idfs, mean_idf),
        sum_idf,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_empty_match_counts() {
        let features = get_tf_idf_features(&HashMap::new());
        assert_eq!(features.max_tf_idf, 0.0);
        assert_eq!(features.sum_tf, 0.0);
    }

    #[test]
    fn test_single_term() {
        let mut counts = HashMap::new();
        counts.insert("rust".to_string(), 3);
        let features = get_tf_idf_features(&counts);
        assert!(features.sum_tf > 0.0);
        assert!(features.sum_idf > 0.0);
        assert_eq!(features.std_tf, 0.0); // single element, std = 0
    }

    #[test]
    fn test_n_documents_positive() {
        assert!(*N_DOCUMENTS > 0.0);
    }
}
