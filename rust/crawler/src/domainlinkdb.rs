use std::{
    collections::{HashMap, HashSet},
    fs,
    io::Write,
    path::{Path, PathBuf},
    sync::{Arc, LazyLock, atomic::AtomicBool},
};

use anyhow::Context;
use fastbloom::AtomicBloomFilter;
use futures_util::future::join_all;
use mwmbl_core::hn_top_domains_filtered::DOMAINS;
use strum::{EnumCount, IntoEnumIterator};
use strum_macros::{Display, EnumCount, EnumIter};
use tokio::{
    io::{AsyncReadExt, AsyncSeekExt, AsyncWriteExt},
    spawn,
    task::JoinHandle, time::sleep,
};

use crate::{
    constants::{DOMAINS_VEC, LINK_DOMAIN_DB_SAVE_INTERVAL, NUM_DOMAINS_IN_BLOOM_FILTER},
    path_creation::bloom_path,
};

#[derive(EnumIter, Display, EnumCount, Hash, PartialEq, Eq, Clone)]
pub enum DomainGroups {
    Github,
    WikipediaEn,
    YcombinatorNews,
    LemmyMl,
    Mastodon,
    Top,
    Other,
}

pub static TOP_DOMAINS: LazyLock<HashSet<String>> =
    LazyLock::new(|| DOMAINS_VEC.iter().take(4000).cloned().collect());

impl DomainGroups {
    pub fn score(&self) -> f32 {
        match self {
            DomainGroups::Github | DomainGroups::WikipediaEn | DomainGroups::YcombinatorNews => {
                10.0
            }
            DomainGroups::Top => 5.0,
            DomainGroups::LemmyMl | DomainGroups::Mastodon => 2.0,
            DomainGroups::Other => 1.0,
        }
    }
}

pub struct DomainLinkDatabase {
    // bool track if the bloom has changed since
    links: HashMap<DomainGroups, (AtomicBloomFilter, AtomicBool)>,
    home: PathBuf,
}

impl DomainLinkDatabase {
    pub async fn new(home: &Path) -> anyhow::Result<Arc<Self>> {
        let mut links: HashMap<DomainGroups, (AtomicBloomFilter, AtomicBool)> =
            HashMap::with_capacity(DomainGroups::COUNT);
        for group in DomainGroups::iter() {
            let bloompath = bloom_path(home, &group);
            fs::create_dir_all(bloompath.parent().context("Invalid bloom path")?)?;

            let mut buffer = Vec::new();
            let mut file = tokio::fs::File::options()
                .read(true)
                .write(true)
                .create(true)
                .truncate(false)
                .open(bloompath)
                .await?;
            file.read_to_end(&mut buffer).await?;

            let bloom: AtomicBloomFilter = if let Ok(bloom) = postcard::from_bytes(&buffer) {
                bloom
            } else {
                let bloom: AtomicBloomFilter = AtomicBloomFilter::with_false_pos(1e-6)
                    .expected_items(NUM_DOMAINS_IN_BLOOM_FILTER);
                file.rewind().await?;
                file.write_all(&postcard::to_stdvec(&bloom)?).await?;
                bloom
            };
            links.insert(group, (bloom, false.into()));
        }

        let db = Arc::new(DomainLinkDatabase {
            links,
            home: home.to_path_buf(),
        });

        let weak = Arc::downgrade(&db);

        spawn(async move {
            loop {
                sleep(LINK_DOMAIN_DB_SAVE_INTERVAL).await;
                if let Some(db) = weak.upgrade() {
                    let mut tasks: Vec<JoinHandle<()>> = Vec::with_capacity(DomainGroups::COUNT);

                    for group in DomainGroups::iter() {
                        let db = db.clone();

                        tasks.push(spawn(async move {
                            let Some((bloom, changed)) = db.links.get(&group) else {
                                eprintln!("Missing group {group}");
                                return;
                            };
                            if !changed.load(std::sync::atomic::Ordering::Relaxed) {
                                return;
                            }
                            let bloompath = bloom_path(&db.home, &group);

                            if let Ok(mut file) = tokio::fs::File::options()
                                .write(true)
                                .create(false)
                                .open(bloompath)
                                .await
                                && let Ok(vec) = postcard::to_stdvec(&bloom)
                            {
                                if file.write_all(&vec).await.is_err() {
                                    eprintln!("Cannot write to bloom file");
                                    return;
                                }
                            } else {
                                eprintln!("Cannot open bloom file");
                                return;
                            }

                            changed.store(false, std::sync::atomic::Ordering::SeqCst);
                        }));
                    }
                    join_all(tasks).await;
                } else {
                    break;
                }
            }
        });
        Ok(db)
    }

    pub fn get_domain_score(&self, domain: &str) -> f32 {
        self.links
            .iter()
            .filter(|(_, (bloom, _))| bloom.contains(domain))
            .map(|(group, _)| group.score())
            .sum()
    }

    pub fn update_domain_links<'a, I>(&self, source: &str, target: I)
    where
        I: Iterator<Item = &'a str> + Sized,
    {
        let group: DomainGroups = match source {
            "github.com" => DomainGroups::Github,
            "en.wikipedia.org" => DomainGroups::WikipediaEn,
            "news.ycombinator.com" => DomainGroups::YcombinatorNews,
            "lemmy.ml" => DomainGroups::LemmyMl,
            "mastodon.social" => DomainGroups::Mastodon,
            _ => {
                if TOP_DOMAINS.contains(source) {
                    DomainGroups::Top
                } else if DOMAINS.contains_key(source) {
                    DomainGroups::Other
                } else {
                    return;
                }
            }
        };

        if let Some((bloom, changed)) = self.links.get(&group) {
            let mut any_change = false;
            for v in target {
                any_change |= !bloom.insert(v);
            }
            if any_change {
                changed.store(true, std::sync::atomic::Ordering::SeqCst);
            }
        }
    }
}

impl Drop for DomainLinkDatabase {
    fn drop(&mut self) {
        for (group, (bloom, changed)) in &self.links {
            let bloompath = bloom_path(&self.home, group);
            if !changed.load(std::sync::atomic::Ordering::SeqCst) {
                continue;
            }

            if let Ok(mut file) = fs::File::options()
                .write(true)
                .create(false)
                .open(bloompath)
                && let Ok(vec) = postcard::to_stdvec(bloom)
            {
                if file.write_all(&vec).is_err() {
                    eprintln!("Cannot write to bloom file (final)");
                }
            } else {
                eprintln!("Cannot open bloom file");
            }
        }
    }
}
