use std::collections::HashSet;

use crate::curated_domains::SharedCuratedDomains;

pub trait BlacklistProvider: Clone {
    fn is_domain_blacklisted(&self, domain: &str) -> anyhow::Result<bool>;
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
    pub fn new() -> BuiltInRulesBlacklistProvider {
        BuiltInRulesBlacklistProvider{}
    }
}

impl BlacklistProvider for BuiltInRulesBlacklistProvider {
    fn is_domain_blacklisted(&self, _domain: &str) -> anyhow::Result<bool> {
        todo!()
    }
}



#[derive(Clone)]
pub struct StaticBlacklistProvider {
    domains: HashSet<String>
}

impl StaticBlacklistProvider {
    #[must_use]
    pub fn new(domains: HashSet<String>) -> StaticBlacklistProvider {
        StaticBlacklistProvider{
            domains
        }
    }
}

impl BlacklistProvider for StaticBlacklistProvider {
    fn is_domain_blacklisted(&self, domain: &str) -> anyhow::Result<bool> {
        Ok(self.domains.contains(domain))
    }
}

#[derive(Clone)]
pub struct SnapshotBlacklist<T: BlacklistProvider = BuiltInRulesBlacklistProvider> {
    redis: redis::Client,
    built_in_rules: T
}

impl<T: BlacklistProvider> SnapshotBlacklist<T> {
    pub fn new(redis: &redis::Client, built_in_rules: T) -> SnapshotBlacklist<T> {
        SnapshotBlacklist{
            redis: redis.clone(),
            built_in_rules
        }
    }
}

impl<T: BlacklistProvider> BlacklistProvider for SnapshotBlacklist<T> {
    fn is_domain_blacklisted(&self, _domain: &str) -> anyhow::Result<bool> {
        todo!();
    }
}


#[derive(Clone)]
pub struct CuratedBlacklistProvider<T: BlacklistProvider = SnapshotBlacklist> {
    parent: T,
    curated_domains: SharedCuratedDomains
}

impl<T: BlacklistProvider> CuratedBlacklistProvider<T> {
    pub fn new(parent: T, curated_domains: SharedCuratedDomains) -> CuratedBlacklistProvider<T> {
        CuratedBlacklistProvider{
            parent,
            curated_domains
        }
    }
}

impl<T: BlacklistProvider> BlacklistProvider for CuratedBlacklistProvider<T> {
    fn is_domain_blacklisted(&self, _domain: &str) -> anyhow::Result<bool> {
        todo!();
    }
}