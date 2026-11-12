use std::sync::Arc;

use mwmbl_core::blacklist_providers::BlacklistProvider;
use scc::HashMap;
use tokio::sync::RwLock;

use crate::{
    CrawlerParams,
    constants::{API_PRODUCT, PROJECT_URL},
    curated_domains::{CuratedDomains, DefaultBlackList, get_default_blacklist_provider},
    url_queue::{SharedURLQueue},
};

pub struct Crawler<T: BlacklistProvider = DefaultBlackList> {
    url_queue: SharedURLQueue<T>,
    blacklist: T,
    username: Arc<RwLock<Option<String>>>,
    error_last_reported_at: Arc<HashMap<String, f32>>,
    params: Arc<CrawlerParams>,
}

impl Crawler {
    pub async fn new(params: CrawlerParams) -> anyhow::Result<Crawler> {
        let username = Arc::new(RwLock::new(None));
        let curated_domains = CuratedDomains::new(&params.remote_server);
        let blacklist = get_default_blacklist_provider(curated_domains.clone()).await?;
        let url_queue = SharedURLQueue::new(blacklist.clone(), curated_domains);

        Ok(Crawler {
            url_queue,
            blacklist,
            username,
            error_last_reported_at: Arc::new(HashMap::default()),
            params: Arc::new(params),
        })
    }

    pub async fn run(&mut self) -> anyhow::Result<()> {
        println!(
            "{:#?}",
            self.blacklist
                .is_domain_blacklisted("lamathematique.ovh")
                .await
        );
        println!(
            "{:#?}",
            self.blacklist
                .is_domain_blacklisted("pornhub.com")
                .await
        );
        let url = self.url_queue.get_url().await;
        let url2 = self.url_queue.get_url().await;
        let url3 = self.url_queue.get_url().await;
        let url4 = self.url_queue.get_url().await;
        println!(
            "{:#?} {:#?} {:#?} {:#?}",
            url.url(), url2.url(), url3.url(), url4.url()
        );

        loop {
            todo!();
        }

    }

    pub async fn run_once(&mut self) -> anyhow::Result<()> {
        todo!();
    }

    pub fn api_user_agent(username: Option<&String>) -> String {
        let identity = if let Some(username) = username {
            format!("+{}; user {}", PROJECT_URL, urlencoding::encode(username))
        } else {
            format!("+{PROJECT_URL}")
        };
        format!("{API_PRODUCT} ({identity})")
    }

    pub async fn fetch_username(username: &mut Arc<RwLock<Option<String>>>) -> Option<String> {
        todo!()
    }
}
