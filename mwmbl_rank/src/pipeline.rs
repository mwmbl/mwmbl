//! The learning-to-rank pipeline: feature extraction, label binarisation at a threshold, and
//! an XGBoost classifier, matching `mwmbl/rankeval/ltr/evaluate.py`.

use std::collections::HashMap;

use anyhow::{Context, Result, bail};
use mwmbl_core::{
    indexer::Document,
    rank::{get_features_regex_unchecked, get_query_regex},
    tokenizer::tokenize,
};
use regex::{Regex, regex};
use url::Url;
use xgb::{
    Booster, DMatrix,
    parameters::{
        BoosterParameters, BoosterParametersBuilder, BoosterType,
        learning::{LearningTaskParametersBuilder, Objective},
        tree::{TreeBoosterParameters, TreeBoosterParametersBuilder, TreeMethod},
    },
};

use crate::features::{
    MATCH_TERMS_INDEX, NUM_FEATURES, NUM_TERMS_INDEX, PROVIDER_FEATURE_NAMES, feature_row,
};

/// What Staan said about a record's URL for its query.
///
/// Keyed on the URL, not on where the record came from, so an index copy of a page Staan
/// also returned carries Staan's rank too.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum StaanRank {
    /// Staan was never asked about this query, as for most extension and curation rows.
    NotAsked,
    /// Staan was asked and did not return this URL.
    NotReturned,
    /// Staan returned this URL at this 0-based rank.
    Rank(u32),
}

impl StaanRank {
    /// What Staan said, from a record's `staan_asked` and `staan_rank` items. A rank that is
    /// missing, not a number or NaN (as pandas writes `None`) means Staan didn't return the URL.
    #[expect(
        clippy::cast_possible_truncation,
        clippy::cast_sign_loss,
        reason = "`as` truncates a fractional rank and saturates one outside u32, which the golden tests pin"
    )]
    pub fn from_items(asked: bool, rank: Option<f64>) -> Self {
        match (asked, rank.filter(|rank| !rank.is_nan())) {
            (false, _) => StaanRank::NotAsked,
            (true, None) => StaanRank::NotReturned,
            (true, Some(rank)) => StaanRank::Rank(rank as u32),
        }
    }

    /// The `in_staan` and `staan_rank` columns, NaN where XGBoost should treat them as missing.
    #[expect(
        clippy::cast_precision_loss,
        reason = "Staan returns a page of results, so its ranks are far below 2^24"
    )]
    fn features(self) -> [f32; 2] {
        match self {
            StaanRank::NotAsked => [f32::NAN, f32::NAN],
            StaanRank::NotReturned => [0.0, f32::NAN],
            StaanRank::Rank(rank) => [1.0, rank as f32],
        }
    }
}

/// A single document record passed from Python.
#[derive(Debug, Clone)]
pub struct DocumentRecord {
    pub query: String,
    pub doc: Document,
    /// Only read by a pipeline with provider features.
    pub staan_rank: StaanRank,
    /// This record is Staan's own result, so it is exempt from the majority-terms filter:
    /// Staan matches semantically, and a low term overlap says less about its results than
    /// about the index's. An index copy of the same URL is not exempt.
    pub from_staan: bool,
}

/// Width of a feature row: the shared features, plus Staan's when `provider_features`.
pub fn num_features(provider_features: bool) -> usize {
    if provider_features {
        NUM_FEATURES + PROVIDER_FEATURE_NAMES.len()
    } else {
        NUM_FEATURES
    }
}

/// The booster attribute recording whether a model was trained with provider features, so a
/// loaded model knows the row width it scores.
const PROVIDER_FEATURES_ATTRIBUTE: &str = "mwmbl_provider_features";

/// A query's terms and their regexes, compiled once per query in a batch.
pub struct QueryTerms {
    terms: Vec<String>,
    text_regex: Regex,
    url_regex: Regex,
}

impl QueryTerms {
    fn new(query: &str) -> Self {
        Self::from_terms(tokenize(&query.to_lowercase()))
    }

    /// Terms used as given, without lowercasing or tokenizing them.
    pub fn from_terms(terms: Vec<String>) -> Self {
        let term_refs: Vec<&str> = terms.iter().map(String::as_str).collect();
        let text_regex =
            get_query_regex(&term_refs, true, false).unwrap_or_else(|_| regex!("").clone());
        let url_regex =
            get_query_regex(&term_refs, true, true).unwrap_or_else(|_| regex!("").clone());
        QueryTerms {
            terms,
            text_regex,
            url_regex,
        }
    }

    /// The features of `doc` for these terms.
    pub fn feature_row(&self, doc: &Document) -> [f32; NUM_FEATURES] {
        let (url_path, url_domain, url_query) = match Url::parse(doc.url()) {
            Ok(url) => (
                url.path().to_owned(),
                url.domain().unwrap_or("").to_owned(),
                url.query().unwrap_or("").to_owned(),
            ),
            Err(_) => (String::new(), String::new(), String::new()),
        };
        let terms: Vec<&str> = self.terms.iter().map(String::as_str).collect();
        let features = get_features_regex_unchecked(
            &terms,
            doc,
            &url_path,
            &url_domain,
            &url_query,
            &self.text_regex,
            &self.url_regex,
        );
        feature_row(features)
    }
}

/// The features of each record, as a flat row-major matrix of `num_features(provider_features)`
/// columns.
pub fn extract_features_batch(records: &[DocumentRecord], provider_features: bool) -> Vec<f32> {
    // Training batches repeat each query many times.
    let mut queries: HashMap<&str, QueryTerms> = HashMap::new();

    let mut features = Vec::with_capacity(records.len() * num_features(provider_features));
    for record in records {
        let query = queries
            .entry(record.query.as_str())
            .or_insert_with(|| QueryTerms::new(&record.query));
        features.extend_from_slice(&query.feature_row(&record.doc));
        if provider_features {
            features.extend_from_slice(&record.staan_rank.features());
        }
    }
    features
}

/// Whether a record matches no more than half the query terms, the heuristic ranker's
/// majority-terms filter.
fn matches_too_few_terms(row: &[f32]) -> Result<bool> {
    let (match_terms, num_terms) = row
        .get(MATCH_TERMS_INDEX)
        .zip(row.get(NUM_TERMS_INDEX))
        .context("A feature row is missing the term counts")?;
    Ok(*match_terms <= num_terms / 2.0)
}

fn check_lengths(
    records: &[DocumentRecord],
    labels: &[f32],
    weights: Option<&[f32]>,
) -> Result<()> {
    if records.is_empty() {
        bail!("No training records provided");
    }
    if records.len() != labels.len() {
        bail!(
            "records length ({}) != labels length ({})",
            records.len(),
            labels.len()
        );
    }
    if let Some(weights) = weights
        && weights.len() != records.len()
    {
        bail!(
            "weights length ({}) != records length ({})",
            weights.len(),
            records.len()
        );
    }
    Ok(())
}

/// The Rust XGBoost pipeline: its hyperparameters, and the booster once trained or loaded.
pub struct XGBPipeline {
    pub threshold: f32,
    pub scale_pos_weight: f32,
    pub reg_lambda: f32,
    pub num_rounds: u32,
    pub max_depth: Option<u32>,
    pub min_child_weight: Option<f32>,
    pub gamma: Option<f32>,
    pub subsample: Option<f32>,
    /// Append Staan's ranking (`PROVIDER_FEATURE_NAMES`) to every row. Set at construction for
    /// training, and read back from the model by `load_model`.
    pub provider_features: bool,
    booster: Option<Booster>,
}

// SAFETY: `Booster` wraps a raw XGBoost handle, so it isn't `Send` automatically. The handle
// isn't tied to the thread that made it, and pyo3 lends the pipeline to one thread at a time.
unsafe impl Send for XGBPipeline {}

impl XGBPipeline {
    #[expect(
        clippy::too_many_arguments,
        reason = "one argument per keyword argument of the Python constructor"
    )]
    pub fn with_params(
        threshold: f32,
        scale_pos_weight: f32,
        reg_lambda: f32,
        num_rounds: u32,
        max_depth: Option<u32>,
        min_child_weight: Option<f32>,
        gamma: Option<f32>,
        subsample: Option<f32>,
        provider_features: bool,
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
            provider_features,
            booster: None,
        }
    }

    /// Trains the model on `labels` binarised at `threshold`, with optional per-row `weights`.
    ///
    /// # Errors
    ///
    /// Fails if there are no records, if `labels` or `weights` don't have one value per
    /// record, or if XGBoost fails.
    pub fn fit(
        &mut self,
        records: &[DocumentRecord],
        labels: &[f32],
        weights: Option<&[f32]>,
    ) -> Result<()> {
        check_lengths(records, labels, weights)?;
        let training_data = self.training_matrix(records, labels, weights)?;
        let mut booster = self.train(&training_data)?;
        booster
            .set_attribute(
                PROVIDER_FEATURES_ATTRIBUTE,
                &self.provider_features.to_string(),
            )
            .context("Failed to set provider features attribute")?;
        self.booster = Some(booster);
        Ok(())
    }

    fn training_matrix(
        &self,
        records: &[DocumentRecord],
        labels: &[f32],
        weights: Option<&[f32]>,
    ) -> Result<DMatrix> {
        let binary_labels: Vec<f32> = labels
            .iter()
            .map(|&label| if label > self.threshold { 1.0 } else { 0.0 })
            .collect();
        let features = extract_features_batch(records, self.provider_features);
        let mut matrix =
            DMatrix::from_dense(&features, records.len()).context("Failed to create DMatrix")?;
        matrix
            .set_labels(&binary_labels)
            .context("Failed to set labels")?;
        if let Some(weights) = weights {
            matrix
                .set_weights(weights)
                .context("Failed to set weights")?;
        }
        Ok(matrix)
    }

    fn train(&self, training_data: &DMatrix) -> Result<Booster> {
        let mut booster = Booster::new_with_cached_dmats(&self.booster_params()?, &[training_data])
            .context("Failed to create Booster")?;
        // `Booster::train` never calls `update` in xgb 3.0.5, so the rounds are run here.
        // XGBoost numbers rounds with an `i32`, so a `num_rounds` past `i32::MAX` wraps
        // negative and trains no rounds.
        for round in 0..self.num_rounds.cast_signed() {
            booster
                .update(training_data, round)
                .with_context(|| format!("XGBoost training failed at round {round}"))?;
        }
        Ok(booster)
    }

    fn booster_params(&self) -> Result<BoosterParameters> {
        let learning_params = LearningTaskParametersBuilder::default()
            .objective(Objective::BinaryLogistic)
            .build()
            .context("Failed to build learning params")?;
        BoosterParametersBuilder::default()
            .booster_type(BoosterType::Tree(self.tree_params()?))
            .learning_params(learning_params)
            .build()
            .context("Failed to build booster params")
    }

    fn tree_params(&self) -> Result<TreeBoosterParameters> {
        let mut builder = TreeBoosterParametersBuilder::default();
        // XGBoost 3 defaults to `hist` on small datasets; the models were tuned with `exact`.
        builder
            .lambda(self.reg_lambda)
            .scale_pos_weight(self.scale_pos_weight)
            .tree_method(TreeMethod::Exact);
        if let Some(max_depth) = self.max_depth {
            builder.max_depth(max_depth);
        }
        if let Some(min_child_weight) = self.min_child_weight {
            builder.min_child_weight(min_child_weight);
        }
        if let Some(gamma) = self.gamma {
            builder.gamma(gamma);
        }
        if let Some(subsample) = self.subsample {
            builder.subsample(subsample);
        }
        builder.build().context("Failed to build tree params")
    }

    /// The probability that each record is relevant, or 0 for one that fails the
    /// majority-terms filter and isn't Staan's own result.
    ///
    /// # Errors
    ///
    /// Fails if the model hasn't been trained or loaded, or if XGBoost fails, as it does on
    /// the infinite features of an empty query.
    pub fn predict(&self, records: &[DocumentRecord]) -> Result<Vec<f32>> {
        let Some(booster) = &self.booster else {
            bail!("Model has not been trained yet. Call fit() first.");
        };
        if records.is_empty() {
            return Ok(vec![]);
        }

        let features = extract_features_batch(records, self.provider_features);
        let matrix =
            DMatrix::from_dense(&features, records.len()).context("Failed to create DMatrix")?;
        let mut predictions = booster
            .predict(&matrix)
            .context("XGBoost prediction failed")?;

        let rows = features.chunks_exact(num_features(self.provider_features));
        for ((prediction, row), record) in predictions.iter_mut().zip(rows).zip(records) {
            if !record.from_staan && matches_too_few_terms(row)? {
                *prediction = 0.0;
            }
        }
        Ok(predictions)
    }

    /// Saves the trained model to `path`, in the format its extension names.
    ///
    /// # Errors
    ///
    /// Fails if the model hasn't been trained or loaded, or if XGBoost can't write the file.
    pub fn save_model(&self, path: &str) -> Result<()> {
        let Some(booster) = &self.booster else {
            bail!("No trained model to save.");
        };
        booster.save(path).context("Failed to save model")
    }

    /// Loads a model from `path`, and sets `provider_features` from its attribute. A model
    /// trained before the attribute existed has none and uses the shared features only.
    ///
    /// # Errors
    ///
    /// Fails if XGBoost can't read the model or its attributes.
    pub fn load_model(&mut self, path: &str) -> Result<()> {
        let booster = Booster::load(path).context("Failed to load model")?;
        let attribute = booster
            .get_attribute(PROVIDER_FEATURES_ATTRIBUTE)
            .context("Failed to read provider features attribute")?;
        self.provider_features = attribute.as_deref() == Some("true");
        self.booster = Some(booster);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn untrained(num_rounds: u32) -> XGBPipeline {
        XGBPipeline::with_params(0.0, 0.1, 2.0, num_rounds, None, None, None, None, false)
    }

    fn make_records(n: usize) -> Vec<DocumentRecord> {
        (0..n)
            .map(|i| DocumentRecord {
                query: "rust programming".to_owned(),
                doc: Document::new(
                    format!("Rust Programming Example {}", i),
                    format!("https://example{}.com/page", i),
                    "A great systems language".to_owned(),
                    Some(i as f32 * 0.1),
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
                staan_rank: StaanRank::NotAsked,
                from_staan: false,
            })
            .collect()
    }

    fn record(url: &str, title: &str, staan_rank: StaanRank, from_staan: bool) -> DocumentRecord {
        DocumentRecord {
            query: "rust programming".to_owned(),
            doc: Document::new(
                title.to_owned(),
                url.to_owned(),
                String::new(),
                Some(1.0),
                None,
                None,
                None,
                None,
                None,
            ),
            staan_rank,
            from_staan,
        }
    }

    #[test]
    fn test_extract_features_batch_shape() {
        let records = make_records(5);
        let flat = extract_features_batch(&records, false);
        assert_eq!(flat.len(), 5 * NUM_FEATURES);
    }

    #[test]
    fn test_provider_features_are_appended() {
        let records = vec![
            record("https://a.com/", "Rust", StaanRank::NotAsked, false),
            record("https://b.com/", "Rust", StaanRank::NotReturned, false),
            record("https://c.com/", "Rust", StaanRank::Rank(3), true),
        ];
        let shared = extract_features_batch(&records, false);
        let flat = extract_features_batch(&records, true);
        let width = num_features(true);
        assert_eq!(width, NUM_FEATURES + 2);
        assert_eq!(flat.len(), 3 * width);

        let rows: Vec<&[f32]> = flat.chunks(width).collect();
        for (i, row) in rows.iter().enumerate() {
            assert_eq!(
                &row[..NUM_FEATURES],
                &shared[i * NUM_FEATURES..(i + 1) * NUM_FEATURES]
            );
        }
        assert!(rows[0][NUM_FEATURES].is_nan() && rows[0][NUM_FEATURES + 1].is_nan());
        assert_eq!(rows[1][NUM_FEATURES], 0.0);
        assert!(rows[1][NUM_FEATURES + 1].is_nan());
        assert_eq!(&rows[2][NUM_FEATURES..], &[1.0, 3.0]);
    }

    #[test]
    fn test_staan_results_are_exempt_from_the_term_filter() {
        let records = make_records(20);
        let mut pipeline = untrained(10);
        pipeline
            .fit(&records, &[1.0; 20], None)
            .expect("fit should succeed");

        // Neither matches a query term; only Staan's own result survives the filter, not
        // an index copy that Staan also returned.
        let test = vec![
            record(
                "https://example.com/cooking",
                "Cooking Recipes",
                StaanRank::Rank(0),
                true,
            ),
            record(
                "https://example.com/cooking",
                "Cooking Recipes",
                StaanRank::Rank(0),
                false,
            ),
        ];
        let preds = pipeline.predict(&test).expect("predict should succeed");
        assert!(preds[0] > 0.0, "Staan's result should keep its prediction");
        assert_eq!(preds[1], 0.0, "the index copy should still be filtered");
    }

    #[test]
    fn test_provider_features_survive_save_and_load() {
        let mut records = make_records(20);
        for (i, rec) in records.iter_mut().enumerate() {
            rec.staan_rank = if i % 2 == 0 {
                StaanRank::Rank(i as u32 / 2)
            } else {
                StaanRank::NotReturned
            };
        }
        let labels: Vec<f32> = (0..20)
            .map(|i| if i % 2 == 0 { 1.0 } else { 0.0 })
            .collect();
        let mut trained = XGBPipeline::with_params(0.0, 0.1, 2.0, 10, None, None, None, None, true);
        trained
            .fit(&records, &labels, None)
            .expect("fit should succeed");

        let path =
            std::env::temp_dir().join(format!("mwmbl-rank-provider-{}.xgb", std::process::id()));
        let path = path.to_str().unwrap();
        trained.save_model(path).expect("save should succeed");

        let mut loaded = untrained(10);
        loaded.load_model(path).expect("load should succeed");
        std::fs::remove_file(path).unwrap();

        assert!(loaded.provider_features);
        assert_eq!(
            loaded.predict(&records).unwrap(),
            trained.predict(&records).unwrap()
        );
    }

    #[test]
    fn test_a_model_without_provider_features_loads_without_them() {
        let records = make_records(20);
        let labels: Vec<f32> = (0..20)
            .map(|i| if i % 2 == 0 { 1.0 } else { 0.0 })
            .collect();
        let mut trained = untrained(10);
        trained
            .fit(&records, &labels, None)
            .expect("fit should succeed");
        let path =
            std::env::temp_dir().join(format!("mwmbl-rank-shared-{}.xgb", std::process::id()));
        let path = path.to_str().unwrap();
        trained.save_model(path).expect("save should succeed");

        let mut loaded = XGBPipeline::with_params(0.0, 0.1, 2.0, 10, None, None, None, None, true);
        loaded.load_model(path).expect("load should succeed");
        std::fs::remove_file(path).unwrap();

        assert!(!loaded.provider_features);
    }

    #[test]
    fn test_pipeline_fit_predict() {
        let records = make_records(20);
        let labels: Vec<f32> = (0..20)
            .map(|i| if i % 2 == 0 { 1.0 } else { 0.0 })
            .collect();

        let mut pipeline = untrained(10);
        pipeline
            .fit(&records, &labels, None)
            .expect("fit should succeed");

        let preds = pipeline.predict(&records).expect("predict should succeed");
        assert_eq!(preds.len(), 20);
        for &p in &preds {
            assert!((0.0..=1.0).contains(&p), "Prediction {} out of [0,1]", p);
        }
    }

    #[test]
    fn test_predict_filters_too_few_match_terms() {
        // Train on records that all match the query, then predict on a mix of
        // matching and non-matching documents. Non-matching docs (match_terms <=
        // num_terms / 2) must be forced to 0.0 regardless of the model output.
        let records = make_records(20);
        let labels: Vec<f32> = vec![1.0; 20];
        let mut pipeline = untrained(10);
        pipeline
            .fit(&records, &labels, None)
            .expect("fit should succeed");

        let test = vec![
            // Matches both query terms ("rust programming") -> kept.
            DocumentRecord {
                query: "rust programming".to_owned(),
                doc: Document::new(
                    "Rust Programming".to_owned(),
                    "https://example.com/rust".to_owned(),
                    "rust programming language".to_owned(),
                    Some(1.0),
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
                staan_rank: StaanRank::NotAsked,
                from_staan: false,
            },
            // Matches no query terms -> filtered to 0.0.
            DocumentRecord {
                query: "rust programming".to_owned(),
                doc: Document::new(
                    "Cooking Recipes".to_owned(),
                    "https://example.com/cooking".to_owned(),
                    "how to bake bread".to_owned(),
                    Some(1.0),
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
                staan_rank: StaanRank::NotAsked,
                from_staan: false,
            },
        ];

        let preds = pipeline.predict(&test).expect("predict should succeed");
        assert_eq!(preds.len(), 2);
        assert!(preds[0] > 0.0, "matching doc should keep its prediction");
        assert_eq!(preds[1], 0.0, "non-matching doc should be filtered to 0.0");
    }

    #[test]
    fn test_fit_with_weights() {
        let records = make_records(20);
        let labels: Vec<f32> = (0..20)
            .map(|i| if i % 2 == 0 { 1.0 } else { 0.0 })
            .collect();

        // Equal explicit weights must reproduce the unweighted model exactly.
        let mut unweighted = untrained(10);
        unweighted
            .fit(&records, &labels, None)
            .expect("fit should succeed");
        let mut weighted = untrained(10);
        weighted
            .fit(&records, &labels, Some(&[1.0; 20]))
            .expect("fit should succeed");
        let base = unweighted
            .predict(&records)
            .expect("predict should succeed");
        let same = weighted.predict(&records).expect("predict should succeed");
        assert_eq!(
            base, same,
            "weight=1.0 for every row must match unweighted training"
        );

        // Near-zero weight on all negative rows must push predictions up.
        let skew: Vec<f32> = labels
            .iter()
            .map(|&l| if l > 0.0 { 1.0 } else { 1e-3 })
            .collect();
        let mut skewed = untrained(10);
        skewed
            .fit(&records, &labels, Some(&skew))
            .expect("fit should succeed");
        let up = skewed.predict(&records).expect("predict should succeed");
        let mean = |v: &[f32]| v.iter().sum::<f32>() / v.len() as f32;
        assert!(
            mean(&up) > mean(&base),
            "down-weighting negatives should raise mean prediction ({} vs {})",
            mean(&up),
            mean(&base)
        );
    }

    #[test]
    fn test_fit_rejects_mismatched_weights() {
        let records = make_records(10);
        let labels = vec![1.0; 10];
        let mut pipeline = untrained(5);
        let result = pipeline.fit(&records, &labels, Some(&[1.0; 3]));
        assert_eq!(
            result.unwrap_err().to_string(),
            "weights length (3) != records length (10)"
        );
    }

    #[test]
    fn test_pipeline_predict_without_fit() {
        let records = make_records(5);
        let pipeline = untrained(10);
        let result = pipeline.predict(&records);
        assert!(result.is_err());
    }

    #[test]
    fn test_pipeline_empty_predict() {
        let records = make_records(10);
        let labels: Vec<f32> = vec![1.0; 10];
        let mut pipeline = untrained(5);
        pipeline
            .fit(&records, &labels, None)
            .expect("fit should succeed");

        let preds = pipeline.predict(&[]).expect("empty predict should succeed");
        assert_eq!(preds.len(), 0);
    }
}
