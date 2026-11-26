use std::{collections::HashSet, future, sync::Arc, time::Duration};

use anyhow::{self, Context, Ok};
use reqwest::Client;
use tokio::{
    sync::RwLock,
    time::{Instant, sleep},
};

use crate::{
    blacklist::domain_and_parents,
    constants::{
        BLACKLIST_REFRESH_CHECK, DOMAIN_BLACKLIST_REGEX, EXCLUDED_DOMAINS, NUMBERS_ONLY_REGEX,
        REMOTE_BLACKLIST_MAX_AGE,
    },
    hn_top_domains_filtered::DOMAINS,
};
extern crate reqwest;

pub trait BlacklistProvider: Clone + Send + Sync {
    fn is_domain_blacklisted(&self, domain: &str) -> impl Future<Output = bool>;
    fn refresh(&mut self) -> impl Future<Output = anyhow::Result<()>> + Send + Sync;
}

#[derive(Clone)]
pub struct BuiltInRulesBlacklistProvider {}

impl Default for BuiltInRulesBlacklistProvider {
    fn default() -> Self {
        Self::new()
    }
}

impl BuiltInRulesBlacklistProvider {
    #[must_use]
    pub fn new() -> Self {
        Self {}
    }
}

impl BlacklistProvider for BuiltInRulesBlacklistProvider {
    fn is_domain_blacklisted(&self, domain: &str) -> impl Future<Output = bool> {
        if EXCLUDED_DOMAINS.contains(domain) {
            return future::ready(true);
        }

        if DOMAIN_BLACKLIST_REGEX.is_match(domain) {
            return future::ready(true);
        }

        if DOMAINS.contains_key(domain) {
            return future::ready(false);
        }

        let domain_parts: Vec<&str> = domain.splitn(3, '.').collect();
        if let Some(first) = domain_parts.first() {
            return future::ready(domain_parts.len() > 2 && NUMBERS_ONLY_REGEX.is_match(first));
        }
        future::ready(false)
    }

    fn refresh(&mut self) -> impl Future<Output = anyhow::Result<()>> {
        future::ready(Ok(()))
    }
}

#[derive(Clone)]
pub struct StaticBlacklistProvider {
    domains: HashSet<String>,
}

impl StaticBlacklistProvider {
    #[must_use]
    pub fn new(domains: HashSet<String>) -> Self {
        Self { domains }
    }
}

impl BlacklistProvider for StaticBlacklistProvider {
    fn is_domain_blacklisted(&self, domain: &str) -> impl Future<Output = bool> {
        future::ready(self.domains.contains(domain))
    }
    fn refresh(&mut self) -> impl Future<Output = anyhow::Result<()>> {
        future::ready(Ok(()))
    }
}

#[derive(Clone)]
pub struct CombinedBlacklistProvider<T: BlacklistProvider, T2: BlacklistProvider> {
    a: T,
    b: T2,
}

impl<T: BlacklistProvider, T2: BlacklistProvider> CombinedBlacklistProvider<T, T2> {
    pub fn new(a: T, b: T2) -> Self {
        Self { a, b }
    }
}

impl<T: BlacklistProvider, T2: BlacklistProvider> BlacklistProvider
    for CombinedBlacklistProvider<T, T2>
{
    async fn is_domain_blacklisted(&self, domain: &str) -> bool {
        self.a.is_domain_blacklisted(domain).await || self.b.is_domain_blacklisted(domain).await
    }
    async fn refresh(&mut self) -> anyhow::Result<()> {
        self.a.refresh().await?;
        self.b.refresh().await
    }
}

#[derive(Clone)]
pub struct RemoteBlacklistProvider {
    url: String,
    // Shared so that SharedSelfRefreshingBlacklistProvider can refresh a cheap clone while
    // crawling goes on against this one.
    domains: Option<Arc<HashSet<String>>>,
    downloaded_at: Option<Instant>,
}

impl RemoteBlacklistProvider {
    #[must_use]
    pub fn new(url: String) -> Self {
        Self {
            url,
            domains: None,
            downloaded_at: None,
        }
    }

    fn is_due(&self, now: Instant) -> bool {
        self.downloaded_at
            .is_none_or(|downloaded_at| now >= downloaded_at + REMOTE_BLACKLIST_MAX_AGE)
    }

    async fn download(url: &str) -> anyhow::Result<HashSet<String>> {
        let response = Client::default()
            .get(url)
            .timeout(Duration::from_mins(1))
            .send()
            .await?;
        let text = response.error_for_status()?.text().await?;
        Ok(parse_list(&text))
    }
}

/// Reads a domain list in either plain or hosts format ("0.0.0.0 domain.com"), skipping
/// comments and blank lines.
fn parse_list(text: &str) -> HashSet<String> {
    let mut domains = HashSet::new();
    for line in text.lines() {
        let without_comment = line.split('#').next().unwrap_or_default();
        let mut fields = without_comment.split_whitespace();
        let Some(first) = fields.next() else {
            continue;
        };
        let domain = if first == "0.0.0.0" || first == "127.0.0.1" {
            fields.next()
        } else {
            Some(first)
        };
        if let Some(domain) = domain {
            domains.insert(domain.to_owned());
        }
    }
    domains
}

impl BlacklistProvider for RemoteBlacklistProvider {
    fn is_domain_blacklisted(&self, domain: &str) -> impl Future<Output = bool> {
        if let Some(domains) = &self.domains {
            return future::ready(domains.contains(domain));
        }
        eprintln!("Warning: Remote Blacklist was used but never loaded!");
        future::ready(false)
    }
    async fn refresh(&mut self) -> anyhow::Result<()> {
        if !self.is_due(Instant::now()) {
            return Ok(());
        }
        // On failure the list already loaded stays in use, and the next check tries again.
        let domains = Self::download(&self.url)
            .await
            .with_context(|| format!("downloading blacklist {}", self.url))?;
        self.domains = Some(Arc::new(domains));
        self.downloaded_at = Some(Instant::now());
        Ok(())
    }
}

#[derive(Clone)]
pub struct SharedSelfRefreshingBlacklistProvider<T: BlacklistProvider> {
    blacklist: Arc<RwLock<T>>,
}

impl<T: BlacklistProvider + 'static> SharedSelfRefreshingBlacklistProvider<T> {
    pub async fn new(blacklist: T) -> anyhow::Result<Self> {
        let mut shared_blacklist = Self {
            blacklist: Arc::new(RwLock::new(blacklist)),
        };
        let weak = Arc::downgrade(&shared_blacklist.blacklist);
        shared_blacklist.refresh().await?;

        tokio::spawn(async move {
            let weak = weak.clone();
            loop {
                sleep(BLACKLIST_REFRESH_CHECK).await;
                let Some(blacklist) = weak.upgrade() else {
                    break;
                };
                // Refresh a copy and swap it in, rather than holding the write lock through
                // the downloads, which would stop every crawl task for up to a minute.
                let mut refreshed = blacklist.read().await.clone();
                match refreshed.refresh().await {
                    Result::Ok(()) => *blacklist.write().await = refreshed,
                    Err(err) => eprintln!("Error while refreshing blacklist: {err:#}"),
                }
            }
        });

        Ok(shared_blacklist)
    }
}

impl<T: BlacklistProvider> BlacklistProvider for SharedSelfRefreshingBlacklistProvider<T> {
    async fn is_domain_blacklisted(&self, domain: &str) -> bool {
        self.blacklist
            .read()
            .await
            .is_domain_blacklisted(domain)
            .await
    }
    async fn refresh(&mut self) -> anyhow::Result<()> {
        self.blacklist.write().await.refresh().await
    }
}

#[derive(Clone)]
pub struct NormalizedBlacklistProvider<T: BlacklistProvider> {
    blacklist: T,
}

impl<T: BlacklistProvider> NormalizedBlacklistProvider<T> {
    pub fn new(blacklist: T) -> Self {
        Self { blacklist }
    }
}

impl<T: BlacklistProvider> BlacklistProvider for NormalizedBlacklistProvider<T> {
    async fn is_domain_blacklisted(&self, domain: &str) -> bool {
        for domain in domain_and_parents(domain) {
            if self.blacklist.is_domain_blacklisted(&domain).await {
                return true;
            }
        }
        false
    }
    async fn refresh(&mut self) -> anyhow::Result<()> {
        self.blacklist.refresh().await
    }
}

#[cfg(test)]
mod tests {
    use std::{collections::HashSet, time::Duration};

    use tokio::time::Instant;

    use super::{RemoteBlacklistProvider, parse_list};
    use crate::constants::REMOTE_BLACKLIST_MAX_AGE;

    #[test]
    fn lists_are_read_line_by_line_in_either_format_without_their_comments() {
        let list = "# Title: Porn Block List\n# Homepage: https://example.org/lists\n\n\
            0.0.0.0 hosts.example\n127.0.0.1 loopback.example # trailing note\n\
            plain.example\n  indented.example  \n0.0.0.0\n";
        let expected: HashSet<String> = [
            "hosts.example",
            "loopback.example",
            "plain.example",
            "indented.example",
        ]
        .into_iter()
        .map(str::to_owned)
        .collect();
        assert_eq!(parse_list(list), expected);
    }

    #[test]
    fn a_list_is_downloaded_again_once_it_is_old() {
        let mut provider = RemoteBlacklistProvider::new("https://example.org/list".to_owned());
        let downloaded_at = Instant::now();
        assert!(provider.is_due(downloaded_at));

        provider.downloaded_at = Some(downloaded_at);
        let almost = downloaded_at + REMOTE_BLACKLIST_MAX_AGE - Duration::from_secs(1);
        assert!(!provider.is_due(almost));
        assert!(provider.is_due(downloaded_at + REMOTE_BLACKLIST_MAX_AGE));
    }
}
