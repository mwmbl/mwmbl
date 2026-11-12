use std::{collections::HashSet, sync::Arc, time::Duration};

use anyhow::Ok;
use mwmbl_core::{
    blacklist::{BlacklistUrls, domain_no_www},
    blacklist_providers::{
        BlacklistProvider, BuiltInRulesBlacklistProvider, CombinedBlacklistProvider,
        NormalizedBlacklistProvider, RemoteBlacklistProvider,
        SharedSelfRefreshingBlacklistProvider,
    }, hn_top_domains_filtered::DOMAINS,
};
use reqwest::Client;
use serde::Deserialize;
use tokio::sync::RwLock;

use crate::crawler::Crawler;

#[derive(Deserialize, Debug)]
pub struct SingleDomainResponse {
    name: String,
}

#[derive(Deserialize, Debug)]
pub struct CuratedDomainsResponse {
    domains: Vec<SingleDomainResponse>,
}

#[derive(Clone)]
pub struct CuratedDomains {
    curated_domains: Arc<RwLock<HashSet<String>>>,
    curated_non_static_domains: Arc<RwLock<Vec<String>>>,
    url: Arc<RwLock<String>>,
}

impl CuratedDomains {
    #[must_use]
    pub fn new(remote_server: &str) -> Self {
        let url = format!("{remote_server}/api/v1/crawler/curated-domains");
        Self {
            curated_domains: Arc::new(RwLock::new(HashSet::default())),
            url: Arc::new(RwLock::new(url)),
            curated_non_static_domains: Arc::new(RwLock::new(Vec::new())),
        }
    }
    
    pub async fn refresh(&mut self) -> anyhow::Result<()> {
        let response = Client::builder()
            .build()?
            .get(self.url.read().await.clone())
            .header("User-Agent", Crawler::api_user_agent(None))
            .timeout(Duration::from_secs(10))
            .send()
            .await?;

        let response: CuratedDomainsResponse = response.error_for_status()?.json().await?;

        let mut curated_domains: HashSet<String> = HashSet::default();
        let mut curated_non_static_domains: Vec<String> = Vec::default();


        for domain in response.domains {
            curated_domains.insert(domain.name);
        }

        for domain in &curated_domains {
            if !DOMAINS.contains_key(domain) {
                curated_non_static_domains.push(domain.clone());
            }
        }

        *self.curated_domains.write().await = curated_domains;
        *self.curated_non_static_domains.write().await = curated_non_static_domains;

        Ok(())
    }
    pub async fn is_domain_curated(&self, domain: &str) -> bool {
        self.curated_domains.read().await.contains(&domain_no_www(domain))
    }
    
    pub fn get_curated_non_static_domains(&self) -> Arc<RwLock<Vec<String>>> {
        self.curated_non_static_domains.clone()
    }
}



#[derive(Clone)]
pub struct CuratedBlacklistProvider<T: BlacklistProvider> {
    parent: T,
    curated_domains: CuratedDomains,
}

impl<T: BlacklistProvider> CuratedBlacklistProvider<T> {
    pub fn new(parent: T, curated_domains: CuratedDomains) -> Self {
        Self {
            parent,
            curated_domains,
        }
    }
}

impl<T: BlacklistProvider> BlacklistProvider for CuratedBlacklistProvider<T> {
    async fn is_domain_blacklisted(&self, domain: &str) -> anyhow::Result<bool> {
        Ok(self.parent.is_domain_blacklisted(domain).await?
            && !self.curated_domains.is_domain_curated(domain).await)
    }
    async fn refresh(&mut self) -> anyhow::Result<()> {
        self.curated_domains.refresh().await?;
        self.parent.refresh().await
    }
}

pub type DefaultBlackList = SharedSelfRefreshingBlacklistProvider<
    CuratedBlacklistProvider<
        NormalizedBlacklistProvider<
            CombinedBlacklistProvider<
                CombinedBlacklistProvider<BuiltInRulesBlacklistProvider, RemoteBlacklistProvider>,
                RemoteBlacklistProvider,
            >,
        >,
    >,
>;

pub async fn get_default_blacklist_provider(curated_domains: CuratedDomains) -> anyhow::Result<DefaultBlackList> {
    let first = BuiltInRulesBlacklistProvider::new();
    let second =
        RemoteBlacklistProvider::new(BlacklistUrls::HageziUrlTifMedium.as_str().to_owned());
    let third = RemoteBlacklistProvider::new(BlacklistUrls::AdultContent.as_str().to_owned());
    let first_second = CombinedBlacklistProvider::new(first, second);
    let combined = CombinedBlacklistProvider::new(first_second, third);
    let normalized = NormalizedBlacklistProvider::new(combined);
    let curated = CuratedBlacklistProvider::new(normalized, curated_domains);
    let refreshing = SharedSelfRefreshingBlacklistProvider::new(curated);
    refreshing.await
}
