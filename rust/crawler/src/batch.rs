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

#[cfg(test)]
mod tests {
    use mwmbl_core::indexer::{Document, DocumentState};
    use serde_json::Value;

    use super::{PostResultsResponse, SendResult, SendResults};

    // The server's side of these is test/test_rust_crawler_contract.py, which checks the
    // same files against the API's schemas.
    const REQUEST: &str = include_str!("../fixtures/results_request.json");
    const RESPONSE: &str = include_str!("../fixtures/results_response.json");

    #[test]
    fn results_are_sent_in_the_shape_the_server_takes() {
        let document = Document::new(
            "Rust ownership explained".to_owned(),
            "https://example.com/rust".to_owned(),
            "How ownership and borrowing work in Rust.".to_owned(),
            Some(1.5),
            Some("rust".to_owned()),
            Some(DocumentState::FromUser),
            Some(vec![3]),
            Some(1_795_000_000),
            None,
        );
        let results = SendResults::new(
            vec![SendResult::new(&document)],
            Some("2.0.0".to_owned()),
            Some("blue-heron".to_owned()),
            Some(true),
        );

        let sent = serde_json::to_value(&results).unwrap();
        let expected: Value = serde_json::from_str(REQUEST).unwrap();
        assert_eq!(sent, expected);
    }

    #[test]
    fn the_server_response_is_read_with_its_curated_states() {
        let response: PostResultsResponse = serde_json::from_str(RESPONSE).unwrap();
        let new_items = response.new_items().unwrap();
        assert_eq!(new_items.len(), 1);
        assert_eq!(new_items[0].state(), Some(&DocumentState::OrganicApproved));
        assert_eq!(new_items[0].user_ids(), Some(&vec![3, 5]));
    }
}
