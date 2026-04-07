/// XGBoost pipeline: feature extraction + threshold binarisation + XGBoost classifier.
/// Replicates make_pipeline(FeatureExtractor(), ThresholdPredictor(0.0, XGBClassifier(...)))
/// from mwmbl/rankeval/ltr/evaluate.py.
///
/// Uses the xgb 3.0.5 crate (prebuilt XGBoost binaries, same API as xgboost 0.1.4).

use xgb::{parameters, Booster, DMatrix};

use crate::features::{get_features_with_regex, NUM_FEATURES};
use crate::text::{tokenize, build_query_regex};

/// A single document record passed from Python.
#[derive(Debug, Clone)]
pub struct DocumentRecord {
    pub query: String,
    pub url: String,
    pub title: String,
    pub extract: String,
    pub score: f32,
}

/// Extract features for a slice of document records.
/// Returns a flat row-major Vec<f32> of shape (n_rows × NUM_FEATURES).
///
/// Optimisation: tokenizes each unique query only once, so a batch where many
/// records share the same query (typical in LTR datasets) avoids redundant work.
/// Per-query cached data: tokenized terms + pre-compiled regexes.
struct QueryCache {
    terms: Vec<String>,
    re_text: Option<regex::Regex>,
    re_url: Option<regex::Regex>,
}

pub fn extract_features_batch(records: &[DocumentRecord]) -> Vec<f32> {
    use std::collections::HashMap;

    // Build a per-query cache of tokenized terms and compiled regexes.
    // This means each unique query compiles its regexes exactly once,
    // regardless of how many records share that query.
    let mut query_cache: HashMap<&str, QueryCache> = HashMap::new();

    let mut flat: Vec<f32> = Vec::with_capacity(records.len() * NUM_FEATURES);
    for rec in records {
        let entry = query_cache.entry(rec.query.as_str()).or_insert_with(|| {
            let terms = tokenize(&rec.query.to_lowercase());
            let term_refs: Vec<&str> = terms.iter().map(|s| s.as_str()).collect();
            let re_text = build_query_regex(&term_refs, true, false);
            let re_url  = build_query_regex(&term_refs, true, true);
            QueryCache { terms, re_text, re_url }
        });

        let terms: Vec<&str> = entry.terms.iter().map(|s| s.as_str()).collect();
        let row = get_features_with_regex(
            &terms,
            &rec.title,
            &rec.url,
            &rec.extract,
            rec.score,
            entry.re_text.as_ref(),
            entry.re_url.as_ref(),
        );
        flat.extend_from_slice(&row);
    }
    flat
}

/// The Rust XGBoost pipeline.
/// Holds the trained booster and hyperparameters.
pub struct XGBPipeline {
    pub threshold: f32,
    pub scale_pos_weight: f32,
    pub reg_lambda: f32,
    pub num_rounds: u32,
    pub max_depth: Option<u32>,
    pub min_child_weight: Option<f32>,
    pub gamma: Option<f32>,
    pub subsample: Option<f32>,
    booster: Option<Booster>,
}

impl XGBPipeline {
    pub fn new(threshold: f32, scale_pos_weight: f32, reg_lambda: f32, num_rounds: u32) -> Self {
        XGBPipeline {
            threshold,
            scale_pos_weight,
            reg_lambda,
            num_rounds,
            max_depth: None,
            min_child_weight: None,
            gamma: None,
            subsample: None,
            booster: None,
        }
    }

    pub fn with_params(
        threshold: f32,
        scale_pos_weight: f32,
        reg_lambda: f32,
        num_rounds: u32,
        max_depth: Option<u32>,
        min_child_weight: Option<f32>,
        gamma: Option<f32>,
        subsample: Option<f32>,
    ) -> Self {
        XGBPipeline {
            threshold,
            scale_pos_weight,
            reg_lambda,
            num_rounds,
            max_depth,
            min_child_weight,
            gamma,
            subsample,
            booster: None,
        }
    }

    /// Train the XGBoost model.
    ///
    /// `records`: list of document records (query + document fields)
    /// `labels`: relevance labels (float); binarised at `self.threshold` before training
    pub fn fit(&mut self, records: &[DocumentRecord], labels: &[f32]) -> Result<(), String> {
        use std::time::Instant;

        eprintln!("[XGBPipeline::fit] Starting fit() with {} records", records.len());

        if records.is_empty() {
            return Err("No training records provided".to_string());
        }
        if records.len() != labels.len() {
            return Err(format!(
                "records length ({}) != labels length ({})",
                records.len(),
                labels.len()
            ));
        }

        // Binarise labels at threshold (ThresholdPredictor logic)
        let t0 = Instant::now();
        let binary_labels: Vec<f32> = labels
            .iter()
            .map(|&l| if l > self.threshold { 1.0 } else { 0.0 })
            .collect();
        let pos = binary_labels.iter().filter(|&&v| v > 0.0).count();
        eprintln!("[XGBPipeline::fit] Labels binarised in {:.2?}: {} positive / {} total",
            t0.elapsed(), pos, binary_labels.len());

        // Extract features
        let t1 = Instant::now();
        let flat_features = extract_features_batch(records);
        let n_rows = records.len();
        eprintln!("[XGBPipeline::fit] Feature extraction done in {:.2?}: {} rows × {} features = {} values",
            t1.elapsed(), n_rows, flat_features.len() / n_rows.max(1), flat_features.len());

        // Build DMatrix
        let t2 = Instant::now();
        let mut dmat = DMatrix::from_dense(&flat_features, n_rows)
            .map_err(|e| format!("Failed to create DMatrix: {}", e))?;
        eprintln!("[XGBPipeline::fit] DMatrix::from_dense done in {:.2?}", t2.elapsed());

        let t3 = Instant::now();
        dmat.set_labels(&binary_labels)
            .map_err(|e| format!("Failed to set labels: {}", e))?;
        eprintln!("[XGBPipeline::fit] DMatrix::set_labels done in {:.2?}", t3.elapsed());

        // Build learning parameters (objective: binary:logistic)
        let t4 = Instant::now();
        let learning_params = parameters::learning::LearningTaskParametersBuilder::default()
            .objective(parameters::learning::Objective::BinaryLogistic)
            .build()
            .map_err(|e| format!("Failed to build learning params: {}", e))?;

        // Build tree parameters: lambda (reg_lambda) is f32 in xgb 3.0.5
        // Explicitly set tree_method=exact to match XGBoost 2.x behaviour
        // (XGBoost 3.0 changed the default from exact→hist for small datasets).
        let mut tree_builder = parameters::tree::TreeBoosterParametersBuilder::default();
        tree_builder
            .lambda(self.reg_lambda)
            .scale_pos_weight(self.scale_pos_weight)
            .tree_method(parameters::tree::TreeMethod::Exact);
        if let Some(v) = self.max_depth {
            tree_builder.max_depth(v);
        }
        if let Some(v) = self.min_child_weight {
            tree_builder.min_child_weight(v);
        }
        if let Some(v) = self.gamma {
            tree_builder.gamma(v);
        }
        if let Some(v) = self.subsample {
            tree_builder.subsample(v);
        }
        let tree_params = tree_builder
            .build()
            .map_err(|e| format!("Failed to build tree params: {}", e))?;

        // Build booster parameters
        let booster_params = parameters::BoosterParametersBuilder::default()
            .booster_type(parameters::BoosterType::Tree(tree_params))
            .learning_params(learning_params)
            .build()
            .map_err(|e| format!("Failed to build booster params: {}", e))?;
        eprintln!("[XGBPipeline::fit] Booster params built in {:.2?}", t4.elapsed());

        // NOTE: Booster::train() in xgb 3.0.5 has a bug where it never calls update().
        // We use the manual training loop instead: create booster, then call update() each round.
        let t5 = Instant::now();
        let mut booster = Booster::new_with_cached_dmats(&booster_params, &[&dmat])
            .map_err(|e| format!("Failed to create Booster: {}", e))?;
        eprintln!("[XGBPipeline::fit] Booster created in {:.2?}", t5.elapsed());

        eprintln!("[XGBPipeline::fit] Starting training loop: {} rounds", self.num_rounds);
        let t6 = Instant::now();
        for i in 0..self.num_rounds as i32 {
            if i % 10 == 0 {
                eprintln!("[XGBPipeline::fit] Round {}/{} ({:.2?} elapsed)",
                    i, self.num_rounds, t6.elapsed());
            }
            booster.update(&dmat, i)
                .map_err(|e| format!("XGBoost training failed at round {}: {}", i, e))?;
        }
        eprintln!("[XGBPipeline::fit] Training loop done in {:.2?}", t6.elapsed());

        self.booster = Some(booster);
        eprintln!("[XGBPipeline::fit] fit() complete");
        Ok(())
    }

    /// Predict probabilities for a batch of records.
    /// Returns a Vec<f32> of class-1 probabilities (one per record).
    pub fn predict(&self, records: &[DocumentRecord]) -> Result<Vec<f32>, String> {
        let booster = self.booster.as_ref()
            .ok_or_else(|| "Model has not been trained yet. Call fit() first.".to_string())?;

        if records.is_empty() {
            return Ok(vec![]);
        }

        let flat_features = extract_features_batch(records);
        let n_rows = records.len();

        let dmat = DMatrix::from_dense(&flat_features, n_rows)
            .map_err(|e| format!("Failed to create DMatrix: {}", e))?;

        let predictions = booster.predict(&dmat)
            .map_err(|e| format!("XGBoost prediction failed: {}", e))?;

        Ok(predictions)
    }

    /// Save the trained model to a file path (XGBoost binary format).
    pub fn save_model(&self, path: &str) -> Result<(), String> {
        let booster = self.booster.as_ref()
            .ok_or_else(|| "No trained model to save.".to_string())?;
        booster.save(path)
            .map_err(|e| format!("Failed to save model: {}", e))
    }

    /// Load a model from a file path (XGBoost binary format).
    pub fn load_model(&mut self, path: &str) -> Result<(), String> {
        let booster = Booster::load(path)
            .map_err(|e| format!("Failed to load model: {}", e))?;
        self.booster = Some(booster);
        Ok(())
    }

    /// Create a new pipeline with a pre-loaded model from disk.
    pub fn from_model_path(
        path: &str,
        threshold: f32,
        scale_pos_weight: f32,
        reg_lambda: f32,
        num_rounds: u32,
        max_depth: Option<u32>,
        min_child_weight: Option<f32>,
        gamma: Option<f32>,
        subsample: Option<f32>,
    ) -> Result<Self, String> {
        let mut pipeline = XGBPipeline::with_params(
            threshold, scale_pos_weight, reg_lambda, num_rounds,
            max_depth, min_child_weight, gamma, subsample,
        );
        pipeline.load_model(path)?;
        Ok(pipeline)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_records(n: usize) -> Vec<DocumentRecord> {
        (0..n).map(|i| DocumentRecord {
            query: "rust programming".to_string(),
            url: format!("https://example{}.com/page", i),
            title: format!("Rust Programming Example {}", i),
            extract: "A great systems language".to_string(),
            score: i as f32 * 0.1,
        }).collect()
    }

    #[test]
    fn test_extract_features_batch_shape() {
        let records = make_records(5);
        let flat = extract_features_batch(&records);
        assert_eq!(flat.len(), 5 * NUM_FEATURES);
    }

    #[test]
    fn test_pipeline_fit_predict() {
        let records = make_records(20);
        let labels: Vec<f32> = (0..20).map(|i| if i % 2 == 0 { 1.0 } else { 0.0 }).collect();

        let mut pipeline = XGBPipeline::new(0.0, 0.1, 2.0, 10);
        pipeline.fit(&records, &labels).expect("fit should succeed");

        let preds = pipeline.predict(&records).expect("predict should succeed");
        assert_eq!(preds.len(), 20);
        for &p in &preds {
            assert!(p >= 0.0 && p <= 1.0, "Prediction {} out of [0,1]", p);
        }
    }

    #[test]
    fn test_pipeline_predict_without_fit() {
        let records = make_records(5);
        let pipeline = XGBPipeline::new(0.0, 0.1, 2.0, 10);
        let result = pipeline.predict(&records);
        assert!(result.is_err());
    }

    #[test]
    fn test_pipeline_empty_predict() {
        let records = make_records(10);
        let labels: Vec<f32> = vec![1.0; 10];
        let mut pipeline = XGBPipeline::new(0.0, 0.1, 2.0, 5);
        pipeline.fit(&records, &labels).expect("fit should succeed");

        let preds = pipeline.predict(&[]).expect("empty predict should succeed");
        assert_eq!(preds.len(), 0);
    }
}
