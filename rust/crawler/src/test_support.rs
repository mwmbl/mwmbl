//! A local HTTP server and scratch directories, so tests crawl nothing outside the machine.

use std::{
    collections::{HashMap, HashSet},
    path::PathBuf,
    sync::{Arc, Mutex},
};

use mwmbl_core::blacklist_providers::StaticBlacklistProvider;
use reqwest::{Client, Url, redirect::Policy};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpListener,
    spawn,
};

pub struct Route {
    status: u16,
    content_type: &'static str,
    body: String,
    location: Option<String>,
}

pub fn html(status: u16, body: &str) -> Route {
    Route {
        status,
        content_type: "text/html",
        body: body.to_owned(),
        location: None,
    }
}

pub fn text(status: u16, body: &str) -> Route {
    Route {
        status,
        content_type: "text/plain",
        body: body.to_owned(),
        location: None,
    }
}

pub fn redirect(location: &str) -> Route {
    Route {
        status: 302,
        content_type: "text/plain",
        body: String::new(),
        location: Some(location.to_owned()),
    }
}

pub struct TestServer {
    port: u16,
    requests: Arc<Mutex<Vec<String>>>,
}

impl TestServer {
    /// Serves each path its route, and 404 for anything else. Every request is one
    /// connection, which is all reqwest needs with `Connection: close`.
    pub async fn start(routes: Vec<(&str, Route)>) -> TestServer {
        let routes: Arc<HashMap<String, Route>> = Arc::new(
            routes
                .into_iter()
                .map(|(path, route)| (path.to_owned(), route))
                .collect(),
        );
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let requests = Arc::new(Mutex::new(Vec::new()));
        let recorded = requests.clone();
        spawn(async move {
            loop {
                let (mut stream, _) = listener.accept().await.unwrap();
                let routes = routes.clone();
                let recorded = recorded.clone();
                spawn(async move {
                    let mut buffer = vec![0; 8192];
                    let read = stream.read(&mut buffer).await.unwrap();
                    let request = String::from_utf8_lossy(&buffer[..read]).into_owned();
                    let path = request.split_whitespace().nth(1).unwrap().to_owned();
                    recorded.lock().unwrap().push(path.clone());
                    let not_found = text(404, "not found");
                    let route = routes.get(&path).unwrap_or(&not_found);
                    let location = route
                        .location
                        .as_ref()
                        .map(|location| format!("Location: {location}\r\n"))
                        .unwrap_or_default();
                    let response = format!(
                        "HTTP/1.1 {} X\r\nContent-Type: {}\r\n{location}Content-Length: {}\r\nConnection: close\r\n\r\n{}",
                        route.status,
                        route.content_type,
                        route.body.len(),
                        route.body
                    );
                    stream.write_all(response.as_bytes()).await.unwrap();
                });
            }
        });
        TestServer { port, requests }
    }

    /// `localhost` rather than 127.0.0.1: the crawler only crawls URLs with a domain.
    pub fn url(&self, path: &str) -> Url {
        Url::parse(&format!("http://localhost:{}{path}", self.port)).unwrap()
    }

    pub fn requests(&self) -> Vec<String> {
        self.requests.lock().unwrap().clone()
    }
}

/// A client that ignores the environment's proxy, so requests reach the test server, and
/// leaves redirects to `fetch`, as the crawler's does.
pub fn client() -> Client {
    Client::builder()
        .no_proxy()
        .redirect(Policy::none())
        .build()
        .unwrap()
}

pub fn no_blacklist() -> StaticBlacklistProvider {
    StaticBlacklistProvider::new(HashSet::new())
}

/// A new, empty directory under the system temp directory, standing in for `$HOME`.
pub fn scratch_home(name: &str) -> PathBuf {
    let path = std::env::temp_dir().join(format!(
        "mwmbl-crawler-test-{name}-{}",
        rand::random::<u64>()
    ));
    std::fs::create_dir_all(&path).unwrap();
    path
}
