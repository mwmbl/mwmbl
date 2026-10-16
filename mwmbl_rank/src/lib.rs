/// mwmbl_rank: Rust extension module for Mwmbl learning-to-rank.
///
/// Exposes RustXGBPipeline to Python via PyO3.
/// Build with: maturin develop  (or maturin build --release)

mod domain;
mod features;
mod idf;
mod index;
mod pipeline;
mod text;
mod wiki;

use pyo3::prelude::*;
use pyo3::exceptions::PyValueError;

use pipeline::{extract_features_batch, num_features, DocumentRecord, StaanRank, XGBPipeline};

/// Convert a Python dict (passed as a Bound<PyAny>) to a DocumentRecord.
fn py_dict_to_record(obj: &Bound<'_, PyAny>) -> PyResult<DocumentRecord> {
    let query: String = obj.get_item("query")
        .map_err(|_| PyValueError::new_err("Record missing 'query' key"))?
        .extract()?;
    let url: String = obj.get_item("url")
        .map_err(|_| PyValueError::new_err("Record missing 'url' key"))?
        .extract()?;
    let title: String = obj.get_item("title")
        .map(|v| v.extract::<String>().unwrap_or_default())
        .unwrap_or_default();
    let extract: String = obj.get_item("extract")
        .map(|v| v.extract::<String>().unwrap_or_default())
        .unwrap_or_default();
    let score: f32 = obj.get_item("score")
        .map(|v| v.extract::<f64>().unwrap_or(0.0) as f32)
        .unwrap_or(0.0);

    // Optional, and only read by a pipeline with provider features. A missing or NaN rank
    // (as pandas writes None) means Staan did not return the URL.
    let staan_asked: bool = obj.get_item("staan_asked")
        .map(|v| v.extract::<bool>().unwrap_or(false))
        .unwrap_or(false);
    let rank: Option<f64> = obj.get_item("staan_rank")
        .map(|v| v.extract::<Option<f64>>().unwrap_or(None))
        .unwrap_or(None)
        .filter(|rank| !rank.is_nan());
    let staan_rank = match (staan_asked, rank) {
        (false, _) => StaanRank::NotAsked,
        (true, None) => StaanRank::NotReturned,
        (true, Some(rank)) => StaanRank::Rank(rank as u32),
    };
    let from_staan: bool = obj.get_item("from_staan")
        .map(|v| v.extract::<bool>().unwrap_or(false))
        .unwrap_or(false);

    Ok(DocumentRecord { query, url, title, extract, score, staan_rank, from_staan })
}

/// Python-visible XGBoost pipeline class.
///
/// Provides a sklearn-compatible interface:
///   - fit(records: list[dict], labels: list[float]) -> self
///   - predict(records: list[dict]) -> list[float]
///   - save_model(path: str) -> None
///   - load_model(path: str) -> None
///
/// Each record dict must have keys: query, url, title, extract, score. Optional keys, for
/// Combined Search:
///   - staan_asked (bool): Staan was asked about this query
///   - staan_rank (int or None): Staan's 0-based rank for this URL, None if not returned
///   - from_staan (bool): the record is Staan's own result, exempt from the term filter
///
/// The first two are features only for a pipeline with provider_features, which a loaded
/// model sets from its own metadata.
///
/// XGBPipeline implements Send (see pipeline.rs), so this class is safe to use
/// from multiple Python threads (e.g. Django worker threads).
#[pyclass(name = "RustXGBPipeline")]
pub struct PyXGBPipeline {
    inner: XGBPipeline,
}

#[pymethods]
impl PyXGBPipeline {
    /// Create a new pipeline.
    ///
    /// Args:
    ///     threshold: label binarisation threshold (default 0.0)
    ///     scale_pos_weight: XGBoost scale_pos_weight (default 0.1)
    ///     reg_lambda: XGBoost reg_lambda (default 2.0)
    ///     num_rounds: number of boosting rounds (default 100)
    ///     max_depth: XGBoost max_depth (default None → XGBoost default of 6)
    ///     min_child_weight: XGBoost min_child_weight (default None → XGBoost default of 1.0)
    ///     gamma: XGBoost gamma / min_split_loss (default None → XGBoost default of 0.0)
    ///     subsample: XGBoost subsample (default None → XGBoost default of 1.0)
    ///     provider_features: append Staan's ranking to the features (default False)
    #[new]
    #[pyo3(signature = (threshold=0.0, scale_pos_weight=0.1, reg_lambda=2.0, num_rounds=100, max_depth=None, min_child_weight=None, gamma=None, subsample=None, provider_features=false))]
    fn new(
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
        PyXGBPipeline {
            inner: XGBPipeline::with_params(
                threshold, scale_pos_weight, reg_lambda, num_rounds,
                max_depth, min_child_weight, gamma, subsample, provider_features,
            ),
        }
    }

    /// Train the model.
    ///
    /// Args:
    ///     records: list of dicts with keys query, url, title, extract, score
    ///     labels: list of float relevance labels
    ///     sample_weight: optional list of per-row weights (default: equal weights)
    ///
    /// Returns self (for chaining).
    #[pyo3(signature = (records, labels, sample_weight=None))]
    fn fit(
        &mut self,
        py: Python<'_>,
        records: &Bound<'_, PyAny>,
        labels: Vec<f32>,
        sample_weight: Option<Vec<f32>>,
    ) -> PyResult<PyObject> {
        let doc_records = self.extract_records(records)?;
        self.inner.fit(&doc_records, &labels, sample_weight.as_deref())
            .map_err(|e| PyValueError::new_err(e))?;
        // Return self as a Python object for sklearn-style chaining
        Ok(py.None())
    }

    /// Predict class-1 probabilities.
    ///
    /// Args:
    ///     records: list of dicts with keys query, url, title, extract, score
    ///
    /// Returns list of float probabilities in [0, 1].
    fn predict(&self, records: &Bound<'_, PyAny>) -> PyResult<Vec<f32>> {
        let doc_records = self.extract_records(records)?;
        self.inner.predict(&doc_records)
            .map_err(|e| PyValueError::new_err(e))
    }

    /// Save the trained model to disk (XGBoost binary format).
    fn save_model(&self, path: &str) -> PyResult<()> {
        self.inner.save_model(path)
            .map_err(|e| PyValueError::new_err(e))
    }

    /// Load a model from disk (XGBoost binary format). Sets provider_features from the model.
    fn load_model(&mut self, path: &str) -> PyResult<()> {
        self.inner.load_model(path)
            .map_err(|e| PyValueError::new_err(e))
    }

    /// Whether this pipeline appends Staan's ranking to the features.
    #[getter]
    fn provider_features(&self) -> bool {
        self.inner.provider_features
    }

    /// The feature matrix `predict` scores, one row per record: NUM_FEATURES floats, plus
    /// the provider features when `provider_features`.
    ///
    /// Exposed so experiments can add features of their own alongside these without
    /// reimplementing the extraction in Python.
    #[staticmethod]
    #[pyo3(signature = (records, provider_features=false))]
    fn extract_features(records: &Bound<'_, PyAny>, provider_features: bool) -> PyResult<Vec<Vec<f32>>> {
        let list: Vec<Bound<'_, PyAny>> = records.extract()?;
        let doc_records = list.iter()
            .map(|item| py_dict_to_record(item))
            .collect::<PyResult<Vec<DocumentRecord>>>()?;
        let flat = extract_features_batch(&doc_records, provider_features);
        Ok(flat.chunks(num_features(provider_features)).map(|row| row.to_vec()).collect())
    }

    /// Return the feature names in the canonical order.
    #[staticmethod]
    fn feature_names() -> Vec<&'static str> {
        features::FEATURE_NAMES.to_vec()
    }

    /// Return the number of features.
    #[staticmethod]
    fn num_features() -> usize {
        features::NUM_FEATURES
    }

    fn __repr__(&self) -> String {
        format!(
            "RustXGBPipeline(threshold={}, scale_pos_weight={}, reg_lambda={}, num_rounds={}, max_depth={:?}, min_child_weight={:?}, gamma={:?}, subsample={:?}, provider_features={})",
            self.inner.threshold,
            self.inner.scale_pos_weight,
            self.inner.reg_lambda,
            self.inner.num_rounds,
            self.inner.max_depth,
            self.inner.min_child_weight,
            self.inner.gamma,
            self.inner.subsample,
            self.inner.provider_features,
        )
    }
}

impl PyXGBPipeline {
    fn extract_records(&self, records: &Bound<'_, PyAny>) -> PyResult<Vec<DocumentRecord>> {
        let list: Vec<Bound<'_, PyAny>> = records.extract()?;
        list.iter()
            .map(|item| py_dict_to_record(item))
            .collect()
    }
}

/// Compute features for a single (query, document) pair.
/// Exposed for testing/debugging from Python.
///
/// Args:
///     terms: list of query terms (already lowercased)
///     title: document title
///     url: document URL
///     extract: document extract/snippet
///     score: document score
///     is_complete: whether the query is complete
///
/// Returns a list of 50 floats.
#[pyfunction]
fn get_features_py(
    terms: Vec<String>,
    title: &str,
    url: &str,
    extract: &str,
    score: f32,
    is_complete: bool,
) -> Vec<f32> {
    let term_refs: Vec<&str> = terms.iter().map(|s| s.as_str()).collect();
    features::get_features(&term_refs, title, url, extract, score, is_complete)
}

/// The mwmbl_rank Python extension module.
#[pymodule]
fn mwmbl_rank(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyXGBPipeline>()?;
    m.add_function(wrap_pyfunction!(get_features_py, m)?)?;
    m.add_function(wrap_pyfunction!(index::read_index_page, m)?)?;
    m.add_function(wrap_pyfunction!(index::write_index_page, m)?)?;
    m.add_function(wrap_pyfunction!(index::pack_index_page, m)?)?;
    m.add("PageError", m.py().get_type_bound::<index::PageError>())?;
    m.add("NUM_FEATURES", features::NUM_FEATURES)?;
    m.add("FEATURE_NAMES", features::FEATURE_NAMES.to_vec())?;
    m.add("PROVIDER_FEATURE_NAMES", features::PROVIDER_FEATURE_NAMES.to_vec())?;
    Ok(())
}
