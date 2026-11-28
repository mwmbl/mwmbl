//! The HTTP API, under `/v1/`. Each route names one of the indexes the server was started
//! with, and takes operations rather than finished pages.

use std::{
    collections::{HashMap, HashSet},
    sync::Arc,
};

use axum::{
    Json, Router,
    extract::{DefaultBodyLimit, Path, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
};
use futures_util::future::join_all;
use mwmbl_core::{
    agreement::Agreement,
    blacklist_providers::StaticBlacklistProvider,
    indexer::{Document, TinyIndex},
};
use serde::{Deserialize, Serialize};
use tower::limit::GlobalConcurrencyLimitLayer;

pub type Indexes = Arc<HashMap<String, TinyIndex>>;

/// The largest request body. Clients chunk batches to fit, and a bound stops one request
/// holding unbounded memory; axum's default of 2 MiB is less than a large crawl batch.
const MAX_BODY_BYTES: usize = 32 * 1024 * 1024;

/// The most requests handled at once. During a deploy's handover requests queue until the
/// lock is taken, so this bounds what they can hold; the rest wait to be read.
const MAX_CONCURRENT_REQUESTS: usize = 64;

pub fn router(indexes: Indexes) -> Router {
    Router::new()
        .route("/v1/health", get(health))
        .route("/v1/{index}/documents", post(index_documents))
        .route("/v1/{index}/pages", post(index_pages))
        .route("/v1/{index}/retrieve", post(retrieve))
        .layer(DefaultBodyLimit::max(MAX_BODY_BYTES))
        .layer(GlobalConcurrencyLimitLayer::new(MAX_CONCURRENT_REQUESTS))
        .with_state(indexes)
}

pub enum ApiError {
    UnknownIndex(String),
    BadRequest(String),
    /// The server stopped before it could take the index's lock.
    Unavailable(anyhow::Error),
    Failed(anyhow::Error),
}

impl ApiError {
    fn of(index: &TinyIndex, error: anyhow::Error) -> ApiError {
        if index.holds_lock() {
            ApiError::Failed(error)
        } else {
            ApiError::Unavailable(error)
        }
    }
}

impl From<anyhow::Error> for ApiError {
    fn from(error: anyhow::Error) -> Self {
        ApiError::Failed(error)
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        match self {
            ApiError::UnknownIndex(name) => {
                (StatusCode::NOT_FOUND, format!("No index named {name}")).into_response()
            }
            ApiError::BadRequest(reason) => (StatusCode::BAD_REQUEST, reason).into_response(),
            ApiError::Unavailable(error) => {
                (StatusCode::SERVICE_UNAVAILABLE, format!("{error:#}")).into_response()
            }
            ApiError::Failed(error) => {
                eprintln!("Index request failed: {error:#}");
                (StatusCode::INTERNAL_SERVER_ERROR, format!("{error:#}")).into_response()
            }
        }
    }
}

/// Answers whether or not the server holds its indexes' locks yet, so that a deploy's
/// healthcheck never waits for the container it replaces.
async fn health() -> &'static str {
    "ok"
}

fn index<'a>(indexes: &'a Indexes, name: &str) -> Result<&'a TinyIndex, ApiError> {
    indexes
        .get(name)
        .ok_or_else(|| ApiError::UnknownIndex(name.to_owned()))
}

#[derive(Deserialize)]
struct DocumentsRequest {
    documents: Vec<Document>,
}

#[derive(Serialize)]
struct DocumentsResponse<'a> {
    stored_urls: &'a HashSet<String>,
    new_urls: &'a HashSet<String>,
    new_page_doc_counts: &'a HashMap<String, usize>,
    agreements: &'a [Agreement],
}

/// Indexes the documents under each of their terms. Clients filter out blacklisted
/// documents before sending them, so nothing is blocked here.
async fn index_documents(
    State(indexes): State<Indexes>,
    Path(name): Path<String>,
    Json(request): Json<DocumentsRequest>,
) -> Result<Response, ApiError> {
    let no_blacklist = StaticBlacklistProvider::new(HashSet::new());
    let index = index(&indexes, &name)?;
    let result = index
        .index_documents(request.documents, &no_blacklist)
        .await
        .map_err(|error| ApiError::of(index, error))?;
    let response = DocumentsResponse {
        stored_urls: result.stored_urls(),
        new_urls: result.new_urls(),
        new_page_doc_counts: result.new_page_doc_counts(),
        agreements: result.agreements(),
    };
    Ok(Json(response).into_response())
}

#[derive(Deserialize)]
struct PagesRequest {
    pages: HashMap<usize, Vec<Document>>,
    /// The one term whose organic documents are marked as synced with the main index.
    /// Python's `index_pages(mark_synced: bool)` marks those of every term on the pages.
    mark_synced_term: Option<String>,
}

#[derive(Serialize)]
struct PagesResponse {
    new_page_doc_counts: HashMap<String, usize>,
}

/// Merges documents into the pages given, with no tokenizing.
async fn index_pages(
    State(indexes): State<Indexes>,
    Path(name): Path<String>,
    Json(request): Json<PagesRequest>,
) -> Result<Json<PagesResponse>, ApiError> {
    let index = index(&indexes, &name)?;
    let num_pages = index.num_pages();
    if let Some(page) = request.pages.keys().find(|page| **page >= num_pages) {
        return Err(ApiError::BadRequest(format!(
            "Page {page} is not in an index of {num_pages} pages"
        )));
    }
    let new_page_doc_counts = index
        .index_pages(request.pages, request.mark_synced_term.as_ref())
        .await
        .map_err(|error| ApiError::of(index, error))?;
    Ok(Json(PagesResponse {
        new_page_doc_counts,
    }))
}

#[derive(Deserialize)]
struct RetrieveRequest {
    terms: Vec<String>,
}

#[derive(Serialize)]
struct RetrieveResponse {
    results: HashMap<String, Vec<Document>>,
}

/// The documents for each term, read in parallel by the terms' owner threads. A page that
/// can't be read costs its term's results, not the whole request, as in Python's retrieve.
async fn retrieve(
    State(indexes): State<Indexes>,
    Path(name): Path<String>,
    Json(request): Json<RetrieveRequest>,
) -> Result<Json<RetrieveResponse>, ApiError> {
    let index = index(&indexes, &name)?;
    let documents = join_all(request.terms.iter().map(|term| index.retrieve(term))).await;
    let mut results = HashMap::new();
    for (term, documents) in request.terms.into_iter().zip(documents) {
        let documents = match documents {
            Ok(documents) => documents,
            Err(error) if !index.holds_lock() => return Err(ApiError::Unavailable(error)),
            Err(error) => {
                eprintln!("Could not retrieve {term}: {error:#}");
                Vec::new()
            }
        };
        results.insert(term, documents);
    }
    Ok(Json(RetrieveResponse { results }))
}
