use std::sync::Arc;

use mwmbl_core::blacklist_providers::BlacklistProvider;
use scc::HashMap;
use tokio::sync::RwLock;

use crate::{
    CrawlerParams,
    constants::{API_PRODUCT, PROJECT_URL},
    curated_domains::{DefaultBlackList, get_default_blacklist_provider},
    url_queue::{SharedURLQueue, URLQueue},
};

pub struct Crawler<T: BlacklistProvider = DefaultBlackList, T2: URLQueue = SharedURLQueue<T>> {
    url_queue: T2,
    blacklist: T,
    username: Arc<RwLock<Option<String>>>,
    error_last_reported_at: Arc<HashMap<String, f32>>,
    params: CrawlerParams,
}

impl Crawler {
    pub async fn new(params: CrawlerParams) -> anyhow::Result<Crawler> {
        let username = Arc::new(RwLock::new(None));
        let blacklist = get_default_blacklist_provider(&params.remote_server).await?;
        let url_queue = SharedURLQueue::new(blacklist.clone());

        Ok(Crawler {
            url_queue,
            blacklist,
            username,
            error_last_reported_at: Arc::new(HashMap::default()),
            params,
        })
    }

    pub async fn run(&mut self) -> anyhow::Result<()> {

        loop {
            println!(
                "{:#?}",
                self.blacklist
                    .is_domain_blacklisted("lamathematique.ovh")
                    .await
            );
        }

        todo!();
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
