use chrono::Utc;
use dom_query::Document;
use itertools::Itertools;
use kreuzberg::{ExtractionConfig, OutputFormat, core::mime, extract_bytes};
use rand::{RngExt, seq::IndexedRandom};
use readabilityrs::ReadabilityOptions;
#[cfg(feature = "servo")]
use std::str::FromStr;
use std::{cmp::min, collections::HashSet};

#[cfg(feature = "servo")]
use crate::constants::TIMEOUT;
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
use servo_fetch::{FetchOptions, SessionBrokerConfig, VisibilityPolicy, extract::ExtractInput};

#[derive(Debug)]
pub enum CrawlResult {
    NoDomainError {
        timestamp: chrono::DateTime<Utc>,
        url: Url,
    },
    RobotsUnavailableError {
        timestamp: chrono::DateTime<Utc>,
        url: Url,
        err: anyhow::Error,
    },
    RobotsDeniedError {
        timestamp: chrono::DateTime<Utc>,
        url: Url,
    },
    AbortError {
        timestamp: chrono::DateTime<Utc>,
        url: Url,
        err: anyhow::Error,
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
        err: anyhow::Error,
    },
    CrawlContent {
        timestamp: chrono::DateTime<Utc>,
        url: Url,
        status: StatusCode,
        resolved_url: Url,
        title: String,
        extract: String,
        links: Vec<Url>,
        extra_links: Vec<Url>,
    },
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
            }
            | crate::retrieve::CrawlResult::CrawlContent {
                resolved_url,
                status,
                ..
            } => (resolved_url.domain(), Some(*status)),
        }
    }

    pub fn url(&self) -> &Url {
        match &self {
            CrawlResult::NoDomainError { url, .. }
            | CrawlResult::RobotsUnavailableError { url, .. }
            | CrawlResult::RobotsDeniedError { url, .. }
            | CrawlResult::AbortError { url, .. }
            | CrawlResult::NoResponseTextError { url, .. }
            | CrawlResult::HtmlParsingError { url, .. }
            | CrawlResult::CrawlContent { url, .. } => url,
        }
    }
    pub fn timestamp(&self) -> &chrono::DateTime<Utc> {
        match &self {
            CrawlResult::NoDomainError { timestamp, .. }
            | CrawlResult::RobotsUnavailableError { timestamp, .. }
            | CrawlResult::RobotsDeniedError { timestamp, .. }
            | CrawlResult::AbortError { timestamp, .. }
            | CrawlResult::NoResponseTextError { timestamp, .. }
            | CrawlResult::HtmlParsingError { timestamp, .. }
            | CrawlResult::CrawlContent { timestamp, .. } => timestamp,
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
            return Ok((String::from_utf8(data)?, status, resolved_url));
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
        .timeout(TIMEOUT)
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
                    err,
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
                    err,
                },
            );
        }
    };

    (
        claimed_url,
        sanitize(&html, resolved_url, status, timestamp, url),
    )
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
                            err: Error::msg("Article missing information"),
                            resolved_url,
                        };
                    }
                }
                None => {
                    return CrawlResult::HtmlParsingError {
                        timestamp,
                        url,
                        status,
                        err: Error::msg("Cannot parse article"),
                        resolved_url,
                    };
                }
            },
            Err(e) => {
                return CrawlResult::HtmlParsingError {
                    timestamp,
                    url,
                    status,
                    err: e.into(),
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

    CrawlResult::CrawlContent {
        timestamp,
        url,
        title,
        extract,
        status,
        links: new_links,
        extra_links,
        resolved_url,
    }
}

fn trim_size(string: &str, size: usize) -> String {
    string.chars().take(size - 1).collect::<String>() + "…"
}

fn select_links(links: HashSet<Url>) -> Vec<Url> {
    let map = links.into_iter()
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
        .take(min(map_len, NUM_URLS_TAKENS))
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
