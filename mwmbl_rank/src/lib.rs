#![cfg_attr(
    not(test),
    deny(
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
    )
)]
#![expect(
    unsafe_op_in_unsafe_fn,
    reason = "pyo3 0.21's #[pyfunction] and #[pymethods] expansions call unsafe functions without an unsafe block"
)]

//! The `mwmbl_rank` Python extension: learning-to-rank features and the XGBoost pipeline, and
//! reading and writing index pages.

mod features;
mod index;
mod pipeline;

use mwmbl_core::{indexer::Document, wiki};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use pipeline::{
    DocumentRecord, QueryTerms, StaanRank, XGBPipeline, extract_features_batch, num_features,
};

/// A pipeline error as a Python `ValueError`, with its context chain in the message.
fn value_error(error: &anyhow::Error) -> PyErr {
    PyValueError::new_err(format!("{error:#}"))
}

/// The item `key` of a record, or a `ValueError` naming the key if it can't be read.
fn required_item<'py>(record: &Bound<'py, PyAny>, key: &str) -> PyResult<Bound<'py, PyAny>> {
    record
        .get_item(key)
        .map_err(|_| PyValueError::new_err(format!("Record missing '{key}' key")))
}

/// The item `key` of a record as a `T`, or `None` if it is missing or of another type.
///
/// Lenient pending the decision in #696.
fn lenient_item<'py, T: FromPyObject<'py>>(record: &Bound<'py, PyAny>, key: &str) -> Option<T> {
    record.get_item(key).and_then(|value| value.extract()).ok()
}

/// Reads a record from anything with string keys, such as a dict. `query` and `url` are
/// required; the other items default when missing or wrongly typed.
impl<'py> FromPyObject<'py> for DocumentRecord {
    fn extract_bound(record: &Bound<'py, PyAny>) -> PyResult<Self> {
        let query = required_item(record, "query")?.extract()?;
        let url = required_item(record, "url")?.extract()?;
        let title = lenient_item(record, "title").unwrap_or_default();
        let extract = lenient_item(record, "extract").unwrap_or_default();
        let score = lenient_item(record, "score").unwrap_or(0.0);
        let staan_asked = lenient_item(record, "staan_asked").unwrap_or(false);
        let rank = lenient_item(record, "staan_rank");
        let from_staan = lenient_item(record, "from_staan").unwrap_or(false);
        let doc = Document::new(
            title,
            url,
            extract,
            Some(score),
            None,
            None,
            None,
            None,
            None,
        );
        Ok(DocumentRecord {
            query,
            doc,
            staan_rank: StaanRank::from_items(staan_asked, rank),
            from_staan,
        })
    }
}

/// The learning-to-rank pipeline, with scikit-learn style `fit` and `predict` methods.
///
/// A record is a dict, or anything else with string keys, holding `query` and `url`, and
/// optionally `title`, `extract` and `score`. Combined Search adds:
///
/// - `staan_asked` (bool): Staan was asked about this query.
/// - `staan_rank` (int or None): Staan's 0-based rank for this URL, None if not returned.
/// - `from_staan` (bool): the record is Staan's own result, exempt from the term filter.
///
/// The first two are features only for a pipeline with `provider_features`, which a loaded
/// model sets from its own metadata. A missing or wrongly typed optional item is read as
/// empty, zero or false.
///
/// It is safe to use from several Python threads, such as Django's workers.
#[pyclass(name = "RustXGBPipeline")]
pub struct PyXGBPipeline {
    inner: XGBPipeline,
}

#[pymethods]
impl PyXGBPipeline {
    /// Create a new pipeline.
    ///
    /// Args:
    ///     `threshold`: the label above which a record counts as relevant
    ///     `scale_pos_weight`, `reg_lambda`: XGBoost's parameters of those names
    ///     `num_rounds`: the number of boosting rounds
    ///     `max_depth`, `min_child_weight`, `gamma`, `subsample`: XGBoost's parameters of
    ///         those names, or None for XGBoost's defaults
    ///     `provider_features`: append Staan's ranking to the features
    #[new]
    #[pyo3(signature = (threshold=0.0, scale_pos_weight=0.1, reg_lambda=2.0, num_rounds=100, max_depth=None, min_child_weight=None, gamma=None, subsample=None, provider_features=false))]
    #[expect(
        clippy::too_many_arguments,
        reason = "one argument per keyword argument of the Python constructor"
    )]
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
                threshold,
                scale_pos_weight,
                reg_lambda,
                num_rounds,
                max_depth,
                min_child_weight,
                gamma,
                subsample,
                provider_features,
            ),
        }
    }

    /// Train the model on records and their relevance labels, with equal weights unless
    /// `sample_weight` gives one per record.
    ///
    /// # Errors
    ///
    /// Raises `ValueError` if a record has no `query` or `url`, which includes a record that
    /// isn't a mapping, and `TypeError` if `records` isn't a sequence or is a string, a `query`
    /// or `url` isn't a string, or `labels` or `sample_weight` isn't a sequence of numbers.
    /// Also raises `ValueError` if there are no
    /// records, if `labels` or `sample_weight` doesn't have one value per record, or if XGBoost
    /// fails.
    #[pyo3(signature = (records, labels, sample_weight=None))]
    #[expect(
        clippy::needless_pass_by_value,
        reason = "pyo3 extracts a list argument as an owned Vec"
    )]
    fn fit(
        &mut self,
        records: &Bound<'_, PyAny>,
        labels: Vec<f32>,
        sample_weight: Option<Vec<f32>>,
    ) -> PyResult<()> {
        let records = extract_records(records)?;
        self.inner
            .fit(&records, &labels, sample_weight.as_deref())
            .map_err(|error| value_error(&error))
    }

    /// The probability that each record is relevant, or 0 for a record that matches no more
    /// than half its query's terms unless it is Staan's own result.
    ///
    /// # Errors
    ///
    /// Raises `ValueError` if the model hasn't been trained or loaded, or if XGBoost fails, and
    /// as `fit` does for a record it can't read.
    fn predict(&self, records: &Bound<'_, PyAny>) -> PyResult<Vec<f32>> {
        let records = extract_records(records)?;
        self.inner
            .predict(&records)
            .map_err(|error| value_error(&error))
    }

    /// Save the trained model to disk, in the format its extension names, such as JSON for
    /// `.json`.
    ///
    /// # Errors
    ///
    /// Raises `ValueError` if the model hasn't been trained or loaded, or if XGBoost can't write
    /// the file.
    fn save_model(&self, path: &str) -> PyResult<()> {
        self.inner
            .save_model(path)
            .map_err(|error| value_error(&error))
    }

    /// Load a model saved by `save_model`. Sets `provider_features` from the model.
    ///
    /// # Errors
    ///
    /// Raises `ValueError` if XGBoost can't read the model or its attributes.
    fn load_model(&mut self, path: &str) -> PyResult<()> {
        self.inner
            .load_model(path)
            .map_err(|error| value_error(&error))
    }

    /// Whether this pipeline appends Staan's ranking to the features.
    #[getter]
    fn provider_features(&self) -> bool {
        self.inner.provider_features
    }

    /// The feature matrix `predict` scores, one row per record: `NUM_FEATURES` floats, plus
    /// the provider features when `provider_features`.
    ///
    /// Exposed so experiments can add features of their own alongside these without
    /// reimplementing the extraction in Python.
    ///
    /// # Errors
    ///
    /// Raises as `fit` does for a record it can't read.
    #[staticmethod]
    #[pyo3(signature = (records, provider_features=false))]
    fn extract_features(
        records: &Bound<'_, PyAny>,
        provider_features: bool,
    ) -> PyResult<Vec<Vec<f32>>> {
        let records = extract_records(records)?;
        let flat = extract_features_batch(&records, provider_features);
        Ok(flat
            .chunks_exact(num_features(provider_features))
            .map(<[f32]>::to_vec)
            .collect())
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

/// Reads a sequence of records, such as a list of dicts.
fn extract_records(records: &Bound<'_, PyAny>) -> PyResult<Vec<DocumentRecord>> {
    records.extract()
}

/// The features of one document for a list of query terms, as `extract_features` computes
/// them for a query. `_is_complete` is ignored.
#[pyfunction]
fn get_features_py(
    terms: Vec<String>,
    title: &str,
    url: &str,
    extract: &str,
    score: f32,
    _is_complete: bool,
) -> Vec<f32> {
    let doc = Document::new(
        title.to_owned(),
        url.to_owned(),
        extract.to_owned(),
        Some(score),
        None,
        None,
        None,
        None,
        None,
    );
    QueryTerms::from_terms(terms).feature_row(&doc).to_vec()
}

/// The normalised Wikipedia page view score of a URL, in [0, 1]: 0 for a page not in the stats.
#[pyfunction]
fn wiki_score(url: &str) -> f64 {
    wiki::get_wiki_score(url)
}

/// The `mwmbl_rank` Python extension module.
#[pymodule]
fn mwmbl_rank(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyXGBPipeline>()?;
    m.add_function(wrap_pyfunction!(get_features_py, m)?)?;
    m.add_function(wrap_pyfunction!(wiki_score, m)?)?;
    m.add_function(wrap_pyfunction!(index::read_index_page, m)?)?;
    m.add_function(wrap_pyfunction!(index::write_index_page, m)?)?;
    m.add_function(wrap_pyfunction!(index::pack_index_page, m)?)?;
    m.add("PageError", m.py().get_type_bound::<index::PageError>())?;
    m.add("NUM_FEATURES", features::NUM_FEATURES)?;
    m.add("FEATURE_NAMES", features::FEATURE_NAMES.to_vec())?;
    m.add(
        "PROVIDER_FEATURE_NAMES",
        features::PROVIDER_FEATURE_NAMES.to_vec(),
    )?;
    Ok(())
}
