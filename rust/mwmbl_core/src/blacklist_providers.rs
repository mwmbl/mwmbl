use std::{collections::HashSet, sync::Arc, time::Duration};

use anyhow::{self, Ok};
use reqwest::Client;
use tokio::{sync::RwLock, time::sleep};

use crate::{
    blacklist::domain_and_parents,
    constants::{
        BLACKLIST_REFRESH_CHECK_SECONDS, DOMAIN_BLACKLIST_REGEX, EXCLUDED_DOMAINS,
        NUMBERS_ONLY_REGEX,
    },
    hn_top_domains_filtered::DOMAINS,
};
extern crate reqwest;

pub trait BlacklistProvider: Clone + Send + Sync {
    fn is_domain_blacklisted(
        &self,
        domain: &str,
    ) -> impl Future<Output = bool>;
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
    async fn is_domain_blacklisted(&self, domain: &str) -> bool {
        if EXCLUDED_DOMAINS.contains(domain) {
            return true;
        }

        if DOMAIN_BLACKLIST_REGEX.is_match(domain) {
            return true;
        }

        if DOMAINS.contains_key(domain) {
            return true;
        }

        let domain_parts: Vec<&str> = domain.splitn(3, '.').collect();
        if let Some(first) = domain_parts.first() {
            return domain_parts.len() > 2 && NUMBERS_ONLY_REGEX.is_match(first);
        }
        false
    }

    async fn refresh(&mut self) -> anyhow::Result<()> {
        Ok(())
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
    async fn is_domain_blacklisted(&self, domain: &str) -> bool {
        self.domains.contains(domain)
    }
    async fn refresh(&mut self) -> anyhow::Result<()> {
        Ok(())
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
        self.a.is_domain_blacklisted(domain).await
            || self.b.is_domain_blacklisted(domain).await
    }
    async fn refresh(&mut self) -> anyhow::Result<()> {
        self.a.refresh().await?;
        self.b.refresh().await
    }
}

#[derive(Clone)]
pub struct RemoteBlacklistProvider {
    url: String,
    domains: Option<HashSet<String>>,
}

impl RemoteBlacklistProvider {
    #[must_use]
    pub fn new(url: String) -> Self {
        Self { url, domains: None }
    }

    async fn get_blacklisted_domains(&mut self) -> anyhow::Result<&HashSet<String>> {
        let res = match self.domains.take() {
            Some(res) => res,
            None => Self::build_domains(self.url.clone()).await?,
        };
        Ok(self.domains.insert(res))
    }

    async fn build_domains(url: String) -> anyhow::Result<HashSet<String>> {
        let response = Client::builder()
            .build()?
            .get(url)
            .timeout(Duration::from_mins(1))
            .send()
            .await?;
        let response = response.error_for_status()?;

        let mut domains: HashSet<String> = HashSet::default();
        for line in response.text().await?.split(' ') {
            let line = line.split('#').next();
            if let Some(line) = line {
                let line = line.trim();
                let mut it = line.split_whitespace();
                if let Some(first) = it.next() {
                    let first = first.trim();
                    if first == "0.0.0.0" || first == "127.0.0.1" {
                        if let Some(second) = it.next() {
                            domains.insert(second.to_owned());
                        }
                    } else {
                        domains.insert(first.to_owned());
                    }
                }
            }
        }
        Ok(domains)
    }
}

impl BlacklistProvider for RemoteBlacklistProvider {
    async fn is_domain_blacklisted(&self, domain: &str) -> bool {
        if let Some(domains) = &self.domains {
            return domains.contains(domain);
        }
        eprintln!("Warning: Remote Blacklist was used but never loaded!");
        return false;
    }
    async fn refresh(&mut self) -> anyhow::Result<()> {
        if self.domains.is_none() {
            self.get_blacklisted_domains().await?;
        }
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
                sleep(Duration::from_secs(BLACKLIST_REFRESH_CHECK_SECONDS)).await;
                if let Some(blacklist) = weak.upgrade() {
                    let mut blacklist = blacklist.write().await;
                    if let Err(err) = blacklist.refresh().await {
                        println!("Error while refreshing blacklist : {err}");
                    }
                } else {
                    break;
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
