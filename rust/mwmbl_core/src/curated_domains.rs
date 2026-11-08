use std::{clone, collections::HashSet, sync::{Arc, RwLock}};

struct CuratedDomains {
    curated_domains_cache: HashSet<String>,
    curated_domains_fetched_at: f32,
}

#[derive(clone::Clone)]
pub struct SharedCuratedDomains {
    curated_domains: Arc<RwLock<CuratedDomains>>
}

impl Default for SharedCuratedDomains {
    fn default() -> Self {
        Self::new()
    }
}

impl SharedCuratedDomains {
    #[must_use]
    pub fn new() -> SharedCuratedDomains {
        SharedCuratedDomains{
            curated_domains: Arc::new(RwLock::new(CuratedDomains { curated_domains_cache: HashSet::default(), curated_domains_fetched_at: 0.})),
        }
    }
}