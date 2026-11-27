//! The HTTP API, under `/v1/`. Each route names one of the indexes the server was started
//! with, and takes operations rather than finished pages.

use std::{
    collections::{HashMap, HashSet},
    sync::Arc,
};

use axum::{
    Json, Router,
    extract::{Path, State},
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

pub type Indexes = Arc<HashMap<String, TinyIndex>>;

pub fn router(indexes: Indexes) -> Router {
    Router::new()
        .route("/v1/health", get(health))
        .route("/v1/{index}/documents", post(index_documents))
        .route("/v1/{index}/pages", post(index_pages))
        .route("/v1/{index}/retrieve", post(retrieve))
        .with_state(indexes)
}

pub enum ApiError {
    UnknownIndex(String),
    Failed(anyhow::Error),
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
    let result = index(&indexes, &name)?
        .index_documents(request.documents, &no_blacklist)
        .await?;
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
    mark_synced: Option<String>,
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
    let new_page_doc_counts = index(&indexes, &name)?
        .index_pages(request.pages, request.mark_synced.as_ref())
        .await?;
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

/// The documents for each term, read in parallel by the terms' owner threads.
async fn retrieve(
    State(indexes): State<Indexes>,
    Path(name): Path<String>,
    Json(request): Json<RetrieveRequest>,
) -> Result<Json<RetrieveResponse>, ApiError> {
    let index = index(&indexes, &name)?;
    let documents = join_all(request.terms.iter().map(|term| index.retrieve(term))).await;
    let results = request
        .terms
        .into_iter()
        .zip(documents)
        .map(|(term, documents)| Ok((term, documents?)))
        .collect::<anyhow::Result<_>>()?;
    Ok(Json(RetrieveResponse { results }))
}
