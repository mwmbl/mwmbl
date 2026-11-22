use mwmbl_core::indexer::{Document, DocumentState};
use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize, Debug)]
pub struct SendResult {
    url: String,
    title: String,
    extract: String,
    score: Option<f32>,
    term: Option<String>,
    state: Option<DocumentState>,
    last_crawled: Option<i64>,
}

impl SendResult {
    pub fn new(doc: &Document) -> Self {
        Self {
            url: doc.url().to_owned(),
            title: doc.title().to_owned(),
            extract: doc.extract().to_owned(),
            score: doc.score(),
            term: doc.term().cloned(),
            state: doc.state().cloned(),
            last_crawled: doc.last_crawled(),
        }
    }
}

#[derive(Serialize, Deserialize, Debug)]
pub struct SendResults {
    results: Vec<SendResult>,
    crawler_version: Option<String>,
    device_name: Option<String>,
    include_new_items: Option<bool>,
}

impl SendResults {
    pub fn new(
        results: Vec<SendResult>,
        crawler_version: Option<String>,
        device_name: Option<String>,
        include_new_items: Option<bool>,
    ) -> Self {
        Self {
            results,
            crawler_version,
            device_name,
            include_new_items,
        }
    }
}

#[derive(Serialize, Deserialize, Debug)]
pub struct PostResultsResponse {
    status: String,
    url: Option<String>,
    new_items: Option<Vec<Document>>,
}

impl PostResultsResponse {
    pub fn new_items(&self) -> Option<&Vec<Document>> {
        self.new_items.as_ref()
    }
}
