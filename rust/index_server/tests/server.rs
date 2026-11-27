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
    task::JoinHandle,
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
    send(router, Request::post(uri), body).await
}

async fn send(
    router: &Router,
    request: axum::http::request::Builder,
    body: Value,
) -> (StatusCode, Value) {
    let request = request
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
    let (stop, server) = start(&path, &socket).await;

    let mut stream = send_documents(&socket, document()).await;
    sleep(Duration::from_millis(100)).await;
    stop.send(()).unwrap();

    assert!(response(&mut stream).await.starts_with("HTTP/1.1 503"));
    stopped(server).await;
    drop(first);
    let index = TinyIndex::from_file(&path, IndexMode::READ, 1).unwrap();
    assert_eq!(index.retrieve("rust").await.unwrap().len(), 0);
}

#[tokio::test]
async fn shutting_down_with_the_lock_applies_the_writes_already_sent() {
    let path = index_path("drain");
    let socket = path.with_extension("sock");
    let (stop, server) = start(&path, &socket).await;
    let mut streams = Vec::new();
    for n in 0..8 {
        let url = format!("https://{n}.rust-lang.org/");
        let doc = json!({"title": format!("Rust {n}"), "url": url, "extract": "rust"});
        streams.push(send_documents(&socket, doc).await);
    }
    // Long enough for the server to read the requests: one sent after shutdown begins is
    // never read, and its connection is closed.
    sleep(Duration::from_millis(100)).await;

    stop.send(()).unwrap();

    for stream in &mut streams {
        assert!(response(stream).await.starts_with("HTTP/1.1 200"));
    }
    stopped(server).await;
    let index = TinyIndex::from_file(&path, IndexMode::READ, 1).unwrap();
    assert_eq!(index.retrieve("rust").await.unwrap().len(), 8);
}

async fn start(
    path: &Path,
    socket: &Path,
) -> (oneshot::Sender<()>, JoinHandle<anyhow::Result<()>>) {
    let (stop, stopped) = oneshot::channel::<()>();
    let paths = [("main".to_owned(), path.to_owned())];
    let socket_path = socket.to_owned();
    let server = tokio::spawn(async move {
        serve(
            &socket_path,
            &paths,
            2,
            async move { stopped.await.unwrap() },
        )
        .await
    });
    sleep(Duration::from_millis(100)).await;
    (stop, server)
}

async fn stopped(server: JoinHandle<anyhow::Result<()>>) {
    timeout(Duration::from_secs(5), server)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
}

async fn send_documents(socket: &Path, document: Value) -> UnixStream {
    let body = json!({"documents": [document]}).to_string();
    let mut stream = UnixStream::connect(socket).await.unwrap();
    let request = format!(
        "POST /v1/main/documents HTTP/1.1\r\nhost: index\r\ncontent-type: application/json\r\n\
         content-length: {}\r\nconnection: close\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(request.as_bytes()).await.unwrap();
    stream
}

async fn response(stream: &mut UnixStream) -> String {
    let mut response = String::new();
    stream.read_to_string(&mut response).await.unwrap();
    response
}

async fn read_page(router: &Router, page: usize) -> (Vec<Value>, String) {
    let (status, body) = send(
        router,
        Request::get(format!("/v1/main/page/{page}")),
        json!({}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let documents = body["documents"].as_array().unwrap().clone();
    (documents, body["version"].as_str().unwrap().to_owned())
}

async fn write_page(
    router: &Router,
    page: usize,
    version: &str,
    documents: &[Value],
) -> (StatusCode, Value) {
    let uri = format!("/v1/main/page/{page}?if_version={version}");
    send(router, Request::put(uri), json!({"documents": documents})).await
}

fn page_bytes(path: &Path, page: usize) -> Vec<u8> {
    let bytes = std::fs::read(path).unwrap();
    bytes[(page + 1) * 4096..(page + 2) * 4096].to_vec()
}

#[tokio::test]
async fn racing_conditional_writes_both_land_after_a_retry() {
    let path = index_path("conditional");
    let router = router(&path);
    // An unknown state and the elements a newer build adds survive the round trip.
    let mut first_document = document();
    first_document["state"] = json!(99);
    first_document["extra"] = json!([1, "newer"]);
    let second_document =
        json!({"title": "Other", "url": "https://other.example/", "extract": "x"});

    let (first_read, first_version) = read_page(&router, 3).await;
    let (second_read, second_version) = read_page(&router, 3).await;
    assert_eq!(first_version, second_version);
    let first_write = [first_read, vec![first_document.clone()]].concat();
    let (status, body) = write_page(&router, 3, &first_version, &first_write).await;
    assert_eq!((status, body), (StatusCode::OK, json!({"num_stored": 1})));

    let written = page_bytes(&path, 3);
    let stale_write = [second_read, vec![second_document.clone()]].concat();
    let (status, _) = write_page(&router, 3, &second_version, &stale_write).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(
        page_bytes(&path, 3),
        written,
        "The stale write changed the page"
    );

    let (retry_read, retry_version) = read_page(&router, 3).await;
    let retry_write = [retry_read, vec![second_document.clone()]].concat();
    let (status, _) = write_page(&router, 3, &retry_version, &retry_write).await;
    assert_eq!(status, StatusCode::OK);
    let (documents, _) = read_page(&router, 3).await;
    assert_eq!(documents.len(), 2);
    assert_eq!(documents[0]["state"], json!(99));
    assert_eq!(documents[0]["extra"], json!([1, "newer"]));
    assert_eq!(documents[1]["url"], second_document["url"]);
}

#[tokio::test]
async fn a_corrupt_page_reads_as_empty_and_a_conditional_write_resets_it() {
    let path = index_path("corrupt_page");
    let router = router(&path);
    let file = std::fs::OpenOptions::new().write(true).open(&path).unwrap();
    file.write_all_at(&[0xff; 4096], 5 * 4096).unwrap();

    let (documents, version) = read_page(&router, 4).await;
    assert!(documents.is_empty());
    let (status, _) = write_page(&router, 4, &version, &[document()]).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(read_page(&router, 4).await.0.len(), 1);

    let (status, _) = send(&router, Request::get("/v1/main/page/16"), json!({})).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    let (status, _) = write_page(&router, 16, &version, &[]).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
}
