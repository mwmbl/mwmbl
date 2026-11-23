use chrono::Utc;
use dom_query::Document;
use itertools::Itertools;
use kreuzberg::{ExtractionConfig, OutputFormat, core::mime, extract_bytes};
use rand::seq::IndexedRandom;
use readabilityrs::ReadabilityOptions;
#[cfg(feature = "servo")]
use std::str::FromStr;
use std::{cmp::max, collections::HashSet};
use tokio::task::spawn_blocking;

#[cfg(feature = "servo")]
use crate::constants::READ_TIMEOUT;
use crate::{
    constants::{MAX_FETCH_SIZE, NUM_EXTRACT_CHARS, NUM_TITLE_CHARS, NUM_URLS_TAKENS},
    robots::{RobotCache, robot_allowed},
    url_queue::ClaimedUrl,
};
use anyhow::{Error, bail};
use futures_util::StreamExt;
use readabilityrs::Readability;
use reqwest::{Client, StatusCode, Url};
#[cfg(feature = "servo")]
use servo_fetch::{FetchOptions, VisibilityPolicy};

#[derive(Debug)]
pub enum CrawlResult {
    NoDomainError {
        timestamp: chrono::DateTime<Utc>,
        url: Url,
    },
    RobotsUnavailableError {
        timestamp: chrono::DateTime<Utc>,
        url: Url,
        _err: anyhow::Error,
    },
    RobotsDeniedError {
        timestamp: chrono::DateTime<Utc>,
        url: Url,
    },
    AbortError {
        timestamp: chrono::DateTime<Utc>,
        url: Url,
        _err: anyhow::Error,
    },
    NoResponseTextError {
        timestamp: chrono::DateTime<Utc>,
        url: Url,
        status: StatusCode,
        resolved_url: Url,
    },
    HtmlParsingError {
        timestamp: chrono::DateTime<Utc>,
        url: Url,
        resolved_url: Url,
        status: StatusCode,
        _err: anyhow::Error,
    },
    CrawlContent(SuccesfullCrawlResult),
}

#[derive(Debug)]
pub struct SuccesfullCrawlResult {
    pub timestamp: chrono::DateTime<Utc>,
    pub url: Url,
    pub status: StatusCode,
    pub resolved_url: Url,
    pub title: String,
    pub extract: String,
    pub links: Vec<Url>,
    pub extra_links: Vec<Url>,
}

impl SuccesfullCrawlResult {
    pub fn to_document(&self) -> mwmbl_core::indexer::Document {
        mwmbl_core::indexer::Document::new(
            self.title.clone(),
            self.url.to_string(),
            self.extract.clone(),
            None,
            None,
            None,
            None,
            Some(self.timestamp.timestamp()),
            None,
        )
    }
}

impl CrawlResult {
    pub fn get_resolved_info(&self) -> (Option<&str>, Option<StatusCode>) {
        match &self {
            crate::retrieve::CrawlResult::NoDomainError { .. }
            | crate::retrieve::CrawlResult::RobotsDeniedError { .. }
            | crate::retrieve::CrawlResult::RobotsUnavailableError { .. }
            | crate::retrieve::CrawlResult::AbortError { .. } => (None, None),
            crate::retrieve::CrawlResult::NoResponseTextError {
                resolved_url,
                status,
                ..
            }
            | crate::retrieve::CrawlResult::HtmlParsingError {
                resolved_url,
                status,
                ..
            } => (resolved_url.domain(), Some(*status)),
            crate::retrieve::CrawlResult::CrawlContent(v) => {
                (v.resolved_url.domain(), Some(v.status))
            }
        }
    }

    pub fn url(&self) -> &Url {
        match &self {
            CrawlResult::NoDomainError { url, .. }
            | CrawlResult::RobotsUnavailableError { url, .. }
            | CrawlResult::RobotsDeniedError { url, .. }
            | CrawlResult::AbortError { url, .. }
            | CrawlResult::NoResponseTextError { url, .. }
            | CrawlResult::HtmlParsingError { url, .. } => url,
            CrawlResult::CrawlContent(v) => &v.url,
        }
    }
    pub fn timestamp(&self) -> &chrono::DateTime<Utc> {
        match &self {
            CrawlResult::NoDomainError { timestamp, .. }
            | CrawlResult::RobotsUnavailableError { timestamp, .. }
            | CrawlResult::RobotsDeniedError { timestamp, .. }
            | CrawlResult::AbortError { timestamp, .. }
            | CrawlResult::NoResponseTextError { timestamp, .. }
            | CrawlResult::HtmlParsingError { timestamp, .. } => timestamp,
            CrawlResult::CrawlContent(v) => &v.timestamp,
        }
    }
}

pub async fn fetch(client: &Client, url: &Url) -> anyhow::Result<(Vec<u8>, StatusCode, Url)> {
    let r = client.get(url.clone()).send().await?;

    let status = r.status();
    let url = r.url().to_owned();

    let mut stream = r.bytes_stream();

    let mut data: Vec<u8> = Vec::new();

    while let Some(item) = stream.next().await {
        data.extend(item?);
        if data.len() > MAX_FETCH_SIZE {
            break;
        }
    }
    Ok((data, status, url))
}

#[allow(clippy::field_reassign_with_default)]
async fn fetch_default(
    client: &Client,
    robots_cache: &mut RobotCache,
    url: &Url,
) -> anyhow::Result<(String, StatusCode, Url)> {
    let (data, status, resolved_url) = fetch(client, url).await?;

    if let Some(domain) = resolved_url.domain()
        && robot_allowed(&resolved_url, domain, client, robots_cache).await?
    {
        let mime = mime::detect_mime_type_from_bytes(&data)?;

        if mime == "text/html" {
            return Ok((
                String::from_utf8_lossy(&data).into_owned(),
                status,
                resolved_url,
            ));
        }
        let mut config = ExtractionConfig::default();
        config.output_format = OutputFormat::Html;
        config.use_cache = false;
        let result = extract_bytes(&data, &mime, &config).await?;
        return Ok((result.content, status, resolved_url));
    }
    bail!("Robots.txt unallowed or missing on resolved domain");
}

#[cfg(feature = "servo")]
async fn fetch_servo(
    client: &Client,
    robots_cache: &mut RobotCache,
    url: &Url,
    user_agent: &str,
) -> anyhow::Result<(String, StatusCode, Url)> {
    let options = &FetchOptions::new(url.as_str())
        .timeout(READ_TIMEOUT)
        .user_agent(user_agent)
        .visibility(VisibilityPolicy::moderate());

    let page = servo_fetch::fetch(options).await?;

    let resolved_url = Url::from_str(&page.url)?;

    if let Some(domain) = resolved_url.domain()
        && robot_allowed(&resolved_url, domain, client, robots_cache).await?
    {
        // Assume 200 code for servo as the value is not exposed: https://github.com/konippi/servo-fetch/issues/586
        return Ok((page.html, StatusCode::OK, resolved_url));
    }
    bail!("Robots.txt unallowed or missing on resolved domain");
}

pub async fn crawl(
    claimed_url: ClaimedUrl,
    mut robots_cache: RobotCache,
    client: Client,
    #[cfg(feature = "servo")] user_agent: String,
) -> (ClaimedUrl, CrawlResult) {
    let url = claimed_url.url().clone();
    let timestamp = Utc::now();
    let Some(domain) = url.domain() else {
        return (claimed_url, CrawlResult::NoDomainError { timestamp, url });
    };

    match robot_allowed(&url, domain, &client, &mut robots_cache).await {
        Ok(true) => {}
        Ok(false) => {
            return (
                claimed_url,
                CrawlResult::RobotsDeniedError { timestamp, url },
            );
        }
        Err(err) => {
            return (
                claimed_url,
                CrawlResult::RobotsUnavailableError {
                    timestamp,
                    url,
                    _err: err,
                },
            );
        }
    }

    let res = cfg_select! {
        feature = "servo" => fetch_servo(&client, &mut robots_cache, &url, &user_agent),
        _ => fetch_default(&client, &mut robots_cache, &url)
    }
    .await;

    let (html, status, resolved_url) = match res {
        Ok(v) => v,
        Err(err) => {
            return (
                claimed_url,
                CrawlResult::AbortError {
                    timestamp,
                    url,
                    _err: err,
                },
            );
        }
    };

    // An error page or a bot challenge is not the page, so neither it nor its links are
    // indexed.
    if !status.is_success() {
        return (
            claimed_url,
            CrawlResult::NoResponseTextError {
                timestamp,
                url,
                status,
                resolved_url,
            },
        );
    }

    let owned_url = url.clone();
    let crawl_result =
        match spawn_blocking(move || sanitize(&html, resolved_url, status, timestamp, owned_url))
            .await
        {
            Ok(r) => r,
            Err(err) => {
                return (
                    claimed_url,
                    CrawlResult::AbortError {
                        timestamp,
                        url,
                        _err: err.into(),
                    },
                );
            }
        };

    (claimed_url, crawl_result)
}

fn sanitize(
    html: &str,
    resolved_url: Url,
    status: StatusCode,
    timestamp: chrono::DateTime<Utc>,
    url: Url,
) -> CrawlResult {
    let read_options = ReadabilityOptions::builder()
        .sanitize_content(true)
        .clean_whitespace(true)
        .clean_styles(true)
        .build();

    let (article_html, article_excerpt, mut article_title) =
        match Readability::new(html, Some(resolved_url.as_str()), Some(read_options)) {
            Ok(article) => match article.parse() {
                Some(article) => {
                    if let (Some(html), Some(excerpt), Some(title)) =
                        (article.content, article.excerpt, article.title)
                    {
                        (html, excerpt, title)
                    } else {
                        return CrawlResult::HtmlParsingError {
                            timestamp,
                            url,
                            status,
                            _err: Error::msg("Article missing information"),
                            resolved_url,
                        };
                    }
                }
                None => {
                    return CrawlResult::HtmlParsingError {
                        timestamp,
                        url,
                        status,
                        _err: Error::msg("Cannot parse article"),
                        resolved_url,
                    };
                }
            },
            Err(e) => {
                return CrawlResult::HtmlParsingError {
                    timestamp,
                    url,
                    status,
                    _err: e.into(),
                    resolved_url,
                };
            }
        };

    let new_links = extract_links_from_html(&article_html, &resolved_url);
    let extra_links = extract_links_from_html(html, &resolved_url);

    let extra_links = select_links(&extra_links - &new_links);
    let new_links = select_links(new_links);

    if article_excerpt.is_empty() {
        return CrawlResult::NoResponseTextError {
            timestamp,
            status,
            url,
            resolved_url,
        };
    }

    if article_title.is_empty() {
        article_title = Document::from(article_html)
            .select_single("h1")
            .text()
            .to_string();
    }

    // Note for the future: look into what we can get with markdown:
    // ideas: source code, all internal sub-titles (heandings), internal references, bold/italic text for keywords, latex math, images
    // secondary indexes only for source code and math latex expressions could be an incredible feature, like "code search" and "math search"

    let extract = trim_size(&article_excerpt, NUM_EXTRACT_CHARS);
    let title = trim_size(&article_title, NUM_TITLE_CHARS);

    CrawlResult::CrawlContent(SuccesfullCrawlResult {
        timestamp,
        url,
        title,
        extract,
        status,
        links: new_links,
        extra_links,
        resolved_url,
    })
}

fn trim_size(string: &str, size: usize) -> String {
    if string.len() > size {
        string.chars().take(size - 1).collect::<String>() + "…"
    } else {
        string.to_owned()
    }
}

fn select_links(links: HashSet<Url>) -> Vec<Url> {
    let map = links
        .into_iter()
        .into_group_map_by(|v| v.domain().map(std::borrow::ToOwned::to_owned));
    let map_len = map.len();
    let mut links: Vec<(usize, Url)> = map
        .into_values()
        .flat_map(|v| {
            v.sample(&mut rand::rng(), NUM_URLS_TAKENS)
                .cloned()
                .enumerate()
                .collect::<Vec<_>>()
        })
        .collect();
    links.sort_by_key(|v| v.0);
    links
        .into_iter()
        .map(|v| v.1)
        .take(max(map_len, NUM_URLS_TAKENS))
        .collect()
}

// From internal servo_fetch code.
fn extract_links_from_html(html: &str, base: &Url) -> HashSet<Url> {
    dom_query::Document::from(html)
        .select("a[href]")
        .iter()
        .filter_map(|el| {
            let href = el.attr("href")?;
            let href = href.trim();
            if href.is_empty() {
                return None;
            }
            let resolved = base.join(href).ok()?;
            let url = matches!(resolved.scheme(), "http" | "https").then_some(resolved)?;
            if url.domain().is_some() {
                Some(url)
            } else {
                None
            }
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use std::collections::HashSet;

    use chrono::Utc;
    use reqwest::{StatusCode, Url};

    use super::{CrawlResult, crawl, extract_links_from_html, sanitize, select_links, trim_size};
    use crate::{
        constants::{NUM_EXTRACT_CHARS, NUM_URLS_TAKENS},
        robots::RobotCache,
        test_support::{TestServer, client, html, text},
        url_queue::ClaimedUrl,
    };

    const ARTICLE: &str = r#"<!doctype html><html><head><title>Rust ownership explained</title>
        <meta name="description" content="How ownership and borrowing work in Rust.">
        </head><body>
        <nav><a href="/about">About</a> <a href="https://elsewhere.example/nav">Elsewhere</a></nav>
        <article><h1>Rust ownership explained</h1>
        <p>Ownership is the set of rules that govern how a Rust program manages memory. Every
        value has an owner, and when the owner goes out of scope the value is dropped. This is
        how Rust achieves memory safety without a garbage collector, and it is checked at
        compile time so it costs nothing at run time.</p>
        <p>Borrowing lets code use a value without taking ownership of it. A reference can be
        shared or mutable, and the borrow checker makes sure the two never overlap, which rules
        out data races. Read <a href="/borrowing">the borrowing chapter</a> or the
        <a href="https://doc.rust-lang.org/book/">Rust book</a> for more.</p>
        </article></body></html>"#;

    fn base() -> Url {
        Url::parse("https://example.com/docs/page").unwrap()
    }

    fn urls(links: &[Url]) -> HashSet<&str> {
        links.iter().map(Url::as_str).collect()
    }

    #[test]
    fn links_resolve_against_the_page_and_only_web_urls_with_a_domain_are_kept() {
        let page = r#"<a href="other">relative</a> <a href="/root">root</a>
            <a href=" https://a.example/x ">spaces</a> <a href="">empty</a>
            <a href="mailto:me@example.com">mail</a> <a href="javascript:void(0)">js</a>
            <a href="http://127.0.0.1/admin">ip</a> <a href="ftp://example.com/f">ftp</a>"#;
        let links = extract_links_from_html(page, &base());
        let links: HashSet<String> = links.iter().map(ToString::to_string).collect();
        let expected: HashSet<String> = [
            "https://example.com/docs/other",
            "https://example.com/root",
            "https://a.example/x",
        ]
        .into_iter()
        .map(str::to_owned)
        .collect();
        assert_eq!(links, expected);
    }

    #[test]
    fn link_selection_caps_how_many_links_one_domain_contributes() {
        let many: HashSet<Url> = (0..200)
            .map(|i| Url::parse(&format!("https://big.example/{i}")).unwrap())
            .chain([Url::parse("https://small.example/").unwrap()])
            .collect();
        let selected = select_links(many);
        assert_eq!(selected.len(), NUM_URLS_TAKENS);
        // Every domain's first pick comes before any domain's second.
        assert!(urls(&selected[..2]).contains("https://small.example/"));
    }

    #[test]
    fn long_text_is_cut_at_a_character_boundary_with_an_ellipsis() {
        assert_eq!(trim_size("short", 10), "short");
        let long = "é".repeat(200);
        let trimmed = trim_size(&long, NUM_EXTRACT_CHARS);
        assert_eq!(trimmed.chars().count(), NUM_EXTRACT_CHARS);
        assert!(trimmed.ends_with('…'));
    }

    #[test]
    fn an_article_gives_its_title_extract_and_links() {
        let result = sanitize(ARTICLE, base(), StatusCode::OK, Utc::now(), base());
        let CrawlResult::CrawlContent(content) = result else {
            panic!("not crawled: {result:?}");
        };
        assert_eq!(content.title, "Rust ownership explained");
        assert!(content.extract.contains("ownership"), "{}", content.extract);
        assert!(content.extract.chars().count() <= NUM_EXTRACT_CHARS);
        assert_eq!(
            urls(&content.links),
            HashSet::from([
                "https://example.com/borrowing",
                "https://doc.rust-lang.org/book/"
            ])
        );
        // Links outside the article are kept apart from those in it.
        assert_eq!(
            urls(&content.extra_links),
            HashSet::from(["https://example.com/about", "https://elsewhere.example/nav"])
        );
        let document = content.to_document();
        assert_eq!(document.url(), "https://example.com/docs/page");
        assert_eq!(document.last_crawled(), Some(content.timestamp.timestamp()));
    }

    #[test]
    fn a_page_with_no_text_is_not_content() {
        let result = sanitize(
            "<html><body></body></html>",
            base(),
            StatusCode::OK,
            Utc::now(),
            base(),
        );
        assert!(
            matches!(
                result,
                CrawlResult::HtmlParsingError { .. } | CrawlResult::NoResponseTextError { .. }
            ),
            "{result:?}"
        );
        assert_eq!(
            result.get_resolved_info(),
            (Some("example.com"), Some(StatusCode::OK))
        );
    }

    #[tokio::test]
    async fn crawling_an_allowed_page_fetches_robots_txt_then_the_page() {
        let server = TestServer::start(vec![
            ("/robots.txt", text(200, "User-agent: *\nAllow: /\n")),
            ("/article", html(200, ARTICLE)),
        ])
        .await;
        let url = server.url("/article");

        let (claimed, result) = crawl(
            ClaimedUrl::unqueued(url.clone()),
            RobotCache::new(),
            client(),
        )
        .await;

        assert_eq!(claimed.url(), &url);
        let CrawlResult::CrawlContent(content) = result else {
            panic!("not crawled: {result:?}");
        };
        assert_eq!(content.status, StatusCode::OK);
        assert_eq!(content.title, "Rust ownership explained");
        assert!(urls(&content.links).contains(server.url("/borrowing").as_str()));
        assert_eq!(server.requests(), vec!["/robots.txt", "/article"]);
    }

    #[tokio::test]
    async fn a_page_robots_txt_disallows_is_never_fetched() {
        let server = TestServer::start(vec![
            ("/robots.txt", text(200, "User-agent: *\nDisallow: /\n")),
            ("/article", html(200, ARTICLE)),
        ])
        .await;
        let (_, result) = crawl(
            ClaimedUrl::unqueued(server.url("/article")),
            RobotCache::new(),
            client(),
        )
        .await;
        assert!(
            matches!(result, CrawlResult::RobotsDeniedError { .. }),
            "{result:?}"
        );
        assert_eq!(server.requests(), vec!["/robots.txt"]);
    }

    #[tokio::test]
    async fn a_page_is_not_fetched_while_robots_txt_is_unavailable() {
        let server = TestServer::start(vec![
            ("/robots.txt", text(503, "")),
            ("/article", html(200, ARTICLE)),
        ])
        .await;
        let (_, result) = crawl(
            ClaimedUrl::unqueued(server.url("/article")),
            RobotCache::new(),
            client(),
        )
        .await;
        assert!(
            matches!(result, CrawlResult::RobotsUnavailableError { .. }),
            "{result:?}"
        );
        assert_eq!(server.requests(), vec!["/robots.txt"]);
    }

    #[tokio::test]
    async fn an_error_page_is_not_content_however_much_text_it_has() {
        let server = TestServer::start(vec![
            ("/robots.txt", text(200, "")),
            ("/article", html(403, ARTICLE)),
        ])
        .await;
        let (_, result) = crawl(
            ClaimedUrl::unqueued(server.url("/article")),
            RobotCache::new(),
            client(),
        )
        .await;
        let CrawlResult::NoResponseTextError { status, .. } = result else {
            panic!("crawled an error page: {result:?}");
        };
        assert_eq!(status, StatusCode::FORBIDDEN);
    }

    #[tokio::test]
    async fn a_url_with_no_domain_is_not_crawled() {
        let url = Url::parse("http://127.0.0.1/").unwrap();
        let (_, result) = crawl(ClaimedUrl::unqueued(url), RobotCache::new(), client()).await;
        assert!(
            matches!(result, CrawlResult::NoDomainError { .. }),
            "{result:?}"
        );
    }
}
