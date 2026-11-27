use std::{
    os::unix::fs::FileExt,
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};

use axum::{
    Router,
    body::Body,
    http::{Request, StatusCode},
};
use http_body_util::BodyExt;
use index_server::{api, ownership, serve};
use mwmbl_core::indexer::{IndexMode, TinyIndex, create_index};
use serde_json::{Value, json};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::UnixStream,
    sync::oneshot,
    time::{sleep, timeout},
};
use tower::ServiceExt;

fn index_path(name: &str) -> PathBuf {
    let path = std::env::temp_dir().join(format!("mwmbl_server_{}_{name}", std::process::id()));
    create_index(&path, 16, true).unwrap();
    path
}

fn router(path: &Path) -> Router {
    let paths = [("main".to_owned(), path.to_owned())];
    api::router(Arc::new(ownership::open_indexes(&paths, 2).unwrap()))
}

fn document() -> Value {
    json!({"title": "Rust", "url": "https://rust-lang.org/", "extract": "rust is a language"})
}

async fn post(router: &Router, uri: &str, body: Value) -> (StatusCode, Value) {
    let request = Request::post(uri)
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap();
    let response = router.clone().oneshot(request).await.unwrap();
    let status = response.status();
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

async fn retrieved(router: &Router, term: &str) -> usize {
    let (status, body) = post(router, "/v1/main/retrieve", json!({"terms": [term]})).await;
    assert_eq!(status, StatusCode::OK);
    body["results"][term].as_array().unwrap().len()
}

#[tokio::test]
async fn indexed_documents_are_retrieved() {
    let path = index_path("round_trip");
    let router = router(&path);

    let (status, body) = post(
        &router,
        "/v1/main/documents",
        json!({"documents": [document()]}),
    )
    .await;

    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["stored_urls"], json!(["https://rust-lang.org/"]));
    assert_eq!(body["new_urls"], json!(["https://rust-lang.org/"]));
    assert_eq!(body["agreements"], json!([]));
    assert_eq!(retrieved(&router, "rust").await, 1);
    let (status, _) = post(&router, "/v1/other/retrieve", json!({"terms": ["rust"]})).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    let (status, _) = post(&router, "/v1/main/pages", json!({"pages": {"16": []}})).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);

    // An unreadable page costs its term's results, not the request's.
    let page = TinyIndex::from_file(&path, IndexMode::READ, 1)
        .unwrap()
        .get_key_page_index("language");
    let file = std::fs::OpenOptions::new().write(true).open(&path).unwrap();
    file.write_all_at(&[0xff; 4096], u64::try_from((page + 1) * 4096).unwrap())
        .unwrap();
    let terms = json!({"terms": ["rust", "language"]});
    let (status, body) = post(&router, "/v1/main/retrieve", terms).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["results"]["language"], json!([]));
}

#[tokio::test]
async fn a_second_server_queues_writes_until_the_first_releases_the_lock() {
    let path = index_path("handover");
    let first = TinyIndex::from_file(&path, IndexMode::WRITE, 1).unwrap();
    let page = first.get_key_page_index("rust");
    let second = router(&path);

    let health = Request::get("/v1/health").body(Body::empty()).unwrap();
    assert_eq!(
        second.clone().oneshot(health).await.unwrap().status(),
        StatusCode::OK
    );
    let mut page_document = document();
    page_document["term"] = json!("rust");
    let pages = json!({"pages": {page.to_string(): [page_document]}});
    let write = tokio::spawn({
        let second = second.clone();
        async move { post(&second, "/v1/main/pages", pages).await }
    });
    sleep(Duration::from_millis(200)).await;
    assert!(
        !write.is_finished(),
        "The write was applied without the lock"
    );

    tokio::task::spawn_blocking(move || drop(first))
        .await
        .unwrap();
    let (status, body) = write.await.unwrap();
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["new_page_doc_counts"], json!({"rust": 1}));
    assert_eq!(retrieved(&second, "rust").await, 1);
}

#[tokio::test]
async fn shutting_down_before_the_lock_is_taken_fails_the_queued_writes() {
    let path = index_path("shutdown");
    let socket = path.with_extension("sock");
    let first = TinyIndex::from_file(&path, IndexMode::WRITE, 1).unwrap();
    let (stop, stopped) = oneshot::channel::<()>();
    let server = tokio::spawn({
        let (socket, path) = (socket.clone(), path.clone());
        async move {
            let paths = [("main".to_owned(), path)];
            serve(&socket, &paths, 2, async move { stopped.await.unwrap() }).await
        }
    });
    sleep(Duration::from_millis(100)).await;

    let body = json!({"documents": [document()]}).to_string();
    let mut stream = UnixStream::connect(&socket).await.unwrap();
    let request = format!(
        "POST /v1/main/documents HTTP/1.1\r\nhost: index\r\ncontent-type: application/json\r\n\
         content-length: {}\r\nconnection: close\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(request.as_bytes()).await.unwrap();
    sleep(Duration::from_millis(100)).await;
    stop.send(()).unwrap();

    let mut response = String::new();
    stream.read_to_string(&mut response).await.unwrap();
    assert!(response.starts_with("HTTP/1.1 503"), "{response}");
    timeout(Duration::from_secs(5), server)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    drop(first);
    let index = TinyIndex::from_file(&path, IndexMode::READ, 1).unwrap();
    assert_eq!(index.retrieve("rust").await.unwrap().len(), 0);
}
