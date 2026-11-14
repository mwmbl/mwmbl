use std::{
    clone, collections::HashSet, future, result, str::FromStr, sync::Arc, time::{Duration, Instant, SystemTimeError, UNIX_EPOCH}
};
use readabilityrs::{MarkdownOptions, ReadabilityOptions};

use crate::{
    constants::{
        MAX_FETCH_SIZE, MAX_REDIRECTS, ROBOTS_CACHE_ERROR_TTL_SECONDS, ROBOTS_CACHE_TTL_SECONDS,
        TIMEOUT,
    },
    url_queue::ClaimedUrl,
};
use anyhow::{Error, bail};
use futures_util::StreamExt;
use markdown::ParseOptions;
use readabilityrs::Readability;
use reqwest::{Client, StatusCode, Url, header, redirect::Policy};
use scc::HashCache;
#[cfg(feature = "servo")]
use servo_fetch::{FetchOptions, SessionBrokerConfig, VisibilityPolicy, extract::ExtractInput};
use stop_words::lookup;
use texting_robots::Robot;
use tokio::{spawn, time::sleep};

#[derive(Debug)]
pub enum CrawlErrors {
    NoDomain {
        timestamp: Instant,
        url: Url,
    },
    RobotsUnavailable {
        timestamp: Instant,
        url: Url,
        err: anyhow::Error,
    },
    RobotsDenied {
        timestamp: Instant,
        url: Url,
    },
    AbortError {
        timestamp: Instant,
        url: Url,
        err: anyhow::Error,
    },
    NoResponseText {
        timestamp: Instant,
        url: Url,
        resolved_url: Url,
    },
    HtmlParsing {
        timestamp: Instant,
        url: Url,
        resolved_url: Url,
        err: anyhow::Error,
    },
    ParagraphParsing {
        timestamp: Instant,
        url: Url,
        resolved_url: Url,
    },
}

#[derive(Debug)]
pub struct CrawlContent {
    timestamp: Instant,
    url: Url,
    resolved_url: Url,
    title: String,
    extract: String,
    links: Vec<Url>,
    extra_links: Vec<Url>,
}

#[derive(Clone)]
pub struct CachedRobotFile {
    cache_ttl: Duration,
    robot_fetched: Instant,
    robot: Option<Arc<Robot>>,
}

impl CachedRobotFile {
    pub fn new(cache_ttl: Duration, user_agent: &str, txt: Option<&[u8]>) -> Self {
        let robot = if let Some(txt) = txt
            && let Ok(robot) = Robot::new(user_agent, txt)
        {
            Some(Arc::new(robot))
        } else {
            None
        };
        Self {
            cache_ttl,
            robot_fetched: Instant::now(),
            robot,
        }
    }
    pub fn can_fetch(&self, url: &Url) -> Option<bool> {
        if let Some(robot) = &self.robot {
            return Some(robot.allowed(url.as_str()));
        }
        None
    }
    pub fn delay(&self) -> Option<f32> {
        if let Some(robot) = &self.robot {
            return robot.delay;
        }
        None
    }
    pub fn is_valid(&self) -> bool {
        self.robot_fetched.duration_since(Instant::now()) < self.cache_ttl
    }
}

#[derive(Clone)]
pub struct RobotCache {
    cache: Arc<HashCache<String, CachedRobotFile>>,
    user_agent: String,
}

impl RobotCache {
    #[must_use]
    pub fn new(user_agent: &str) -> Self {
        RobotCache {
            cache: Arc::new(HashCache::new()),
            user_agent: user_agent.to_owned(),
        }
    }

    pub async fn can_fetch(&mut self, domain: &str, url: &Url) -> Option<bool> {
        if let Some(occupied_entry) = self.cache.get_async(domain).await {
            return if occupied_entry.get().is_valid() {
                let _ = occupied_entry.remove();
                None
            } else {
                occupied_entry.get().can_fetch(url)
            };
        }
        None
    }

    pub async fn put(&mut self, domain: &str, file: CachedRobotFile) {
        let _ = self.cache.put_async(domain.to_owned(), file).await;
    }

    pub fn user_agent(&self) -> &str {
        &self.user_agent
    }
}

fn get_robot_url(url: &Url) -> Url {
    let mut robot_path = url.clone();
    robot_path.set_path("robots.txt");
    robot_path.set_query(None);
    robot_path.set_fragment(None);
    robot_path
}

async fn fetch(client: &Client, url: &Url) -> anyhow::Result<(Vec<u8>, StatusCode, Url)> {
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

async fn fetch_default(client: &Client, url: &Url) -> anyhow::Result<(String, Url)> {
    let (data, _, url) = fetch(client, url).await?;
    let data = match String::from_utf8(data) {
        Ok(str) => str,
        Err(err) => err.as_bytes().iter().map(|x| char::from(*x)).collect(),
    };
    Ok((data, url))
}

#[cfg(feature = "servo")]
async fn fetch_servo(url: &Url, user_agent: &str) -> anyhow::Result<(String, Url)> {
     let options = &FetchOptions::new(url.as_str()).timeout(TIMEOUT).user_agent(user_agent).visibility(VisibilityPolicy::moderate());
    
    let page = servo_fetch::fetch(options).await?;

    let resolved_url =  Url::from_str(&page.url)?;
    Ok((page.html, resolved_url))
}

async fn robot_allowed(
    url: &Url,
    domain: &str,
    client: &Client,
    robots_cache: &mut RobotCache,
) -> anyhow::Result<bool> {
    let robot_url = get_robot_url(url);

    if let Some(can_fetch) = robots_cache.can_fetch(domain, url).await {
        return Ok(can_fetch);
    }

    if let Ok((data, status, _)) = fetch(client, &robot_url).await
        && status.is_success()
    {
        let robot = CachedRobotFile::new(
            ROBOTS_CACHE_TTL_SECONDS,
            &robots_cache.user_agent,
            Some(&data),
        );
        let allowed = robot.can_fetch(url).unwrap_or_default();
        robots_cache.put(domain, robot).await;
        return Ok(allowed);
    }
    robots_cache
        .put(
            domain,
            CachedRobotFile::new(
                ROBOTS_CACHE_ERROR_TTL_SECONDS,
                &robots_cache.user_agent,
                None,
            ),
        )
        .await;
    bail!("robots.txt unavailable for {domain}");
}

pub async fn crawl(
    claimed_url: ClaimedUrl,
    mut robots_cache: RobotCache,
    client: Client,
) -> (ClaimedUrl,Result<CrawlContent, CrawlErrors>) {
    let url = claimed_url.url().clone();
    let timestamp = Instant::now();
    let Some(domain) = url.domain() else {
        return (claimed_url,Err(CrawlErrors::NoDomain {
            timestamp,
            url,
        }));
    };

    match robot_allowed(&url, domain, &client, &mut robots_cache).await {
        Ok(true) => {}
        Ok(false) => {
            return (claimed_url, Err(CrawlErrors::RobotsDenied {
                timestamp,
                url,
            }));
        }
        Err(err) => {
            return (claimed_url,Err(CrawlErrors::RobotsUnavailable {
                timestamp,
                url,
                err,
            }));
        }
    }

    let res = cfg_select! {
        feature = "servo" => fetch_servo(&url, &robots_cache.user_agent),
        _ => fetch_default(&client, &url)
    }.await;

    let (html, resolved_url) = match res {
        Ok(v) => v,
        Err(err) => {
            return (claimed_url,Err(CrawlErrors::AbortError {
                timestamp,
                url,
                err,
            }));
        }
    };

    
    let read_options = ReadabilityOptions::builder().sanitize_content(true).clean_whitespace(true).clean_styles(true).build();

    let (article_html, article_text, article_title, article_lang) = match Readability::new(&html, Some(resolved_url.as_str()), Some(read_options)) {
        Ok(article) => {
            match article.parse() {
                Some(article) => {
                    if let (Some(html), Some(text_content), Some(title)) = (article.content, article.text_content, article.title) {
                        (html, text_content, title, article.lang)
                    } else {
                        return (claimed_url,Err(CrawlErrors::HtmlParsing {
                            timestamp,
                            url,
                            err: Error::msg("Article missing information"),
                            resolved_url,
                        }))
                    }
                },
                None => return (claimed_url,Err(CrawlErrors::HtmlParsing {
                    timestamp,
                    url,
                    err: Error::msg("Cannot parse article"),
                    resolved_url,
                }))
            }
        },
        Err(e) => {
            return (claimed_url,Err(CrawlErrors::HtmlParsing {
                timestamp,
                url,
                err: e.into(),
                resolved_url,
            }));
        },
    };

    let new_links = extract_links_from_html(&article_html, &resolved_url);
    let extra_links = extract_links_from_html(&html, &resolved_url);
    let mut extra_links: Vec<Url> = (&extra_links-&new_links).into_iter().take(50).collect();
    extra_links.sort();
    let mut new_links: Vec<Url> = new_links.into_iter().take(50).collect();
    new_links.sort();


    if article_text.is_empty() {
        return (claimed_url,Err(CrawlErrors::NoResponseText {
            timestamp,
            url,
            resolved_url,
        }));
    }

    let words = if let Some(lang) = article_lang && let Some(lang) = stop_words::lookup(lang) {
        lang
    } else {
        let Some(words) = stop_words::lookup(stop_words::Language::English) else {
            return (claimed_url,Err(CrawlErrors::ParagraphParsing{ timestamp, url, resolved_url }))
        };
        words
    };
    let extract = clean_text(&article_text, words);

    (claimed_url,Ok(CrawlContent { timestamp: Instant::now(), url, title: article_title, extract, links: new_links, extra_links, resolved_url }))
}

fn clean_text(text: &str, stop_words: &[&str]) -> String {
    let lowercase_doc = text.to_ascii_lowercase();
    
    let regex_for_punctuation = human_regex::one_or_more(human_regex::punctuation());
    let text_without_punctuation = regex_for_punctuation
        .to_regex()
        .replace_all(&lowercase_doc, "");

    // Make a regex to match stopwords with trailing spaces and punctuation
    let regex_for_stop_words = human_regex::word_boundary()
        + human_regex::exactly(1, human_regex::or(stop_words))
        + human_regex::word_boundary()
        + human_regex::one_or_more(human_regex::whitespace());

    // Remove stop words
    let clean_text = regex_for_stop_words
        .to_regex()
        .replace_all(&text_without_punctuation, "");
    clean_text.split_whitespace().filter(|p| !p.is_empty()).fold(String::new(), |acc,elem| acc + elem + " ").trim_end().to_owned()
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
            if url.domain().is_some() { Some(url) } else { None }
        })
        .collect()
}