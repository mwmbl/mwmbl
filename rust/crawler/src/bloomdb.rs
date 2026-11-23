use std::{
    cmp::max,
    collections::HashSet,
    fs,
    io::Write,
    path::{Path, PathBuf},
    sync::{Arc, LazyLock, atomic::AtomicBool},
};

use anyhow::Context;
use chrono::{DateTime, Datelike, Months, NaiveDate, Utc};
use fastbloom::AtomicBloomFilter;
use mwmbl_core::hn_top_domains_filtered::DOMAINS;
use reqwest::Url;
use strum::IntoEnumIterator;
use strum_macros::{Display, EnumIter};
use tokio::{
    io::{AsyncReadExt, AsyncSeekExt, AsyncWriteExt},
    spawn,
    time::sleep,
};

use crate::{
    constants::{BLOOM_DB_SAVE_INTERVAL, DOMAINS_VEC, NUM_DOMAINS_IN_BLOOM_FILTER},
    path_creation::bloom_path,
    retrieve::CrawlResult,
};

#[derive(EnumIter, Display, strum_macros::EnumCount, Hash, PartialEq, Eq, Clone)]
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

#[derive(EnumIter, Display, strum_macros::EnumCount, Hash, PartialEq, Eq, Clone, Debug)]
pub enum UrlStatus {
    New,
    Queued,
    Assigned,
    ErrorTimeout,
    Error404,
    ErrorOther,
    ErrorRobotsDenied,
    Crawled,
}

impl UrlStatus {
    pub fn crawled(&self) -> bool {
        !matches!(
            self,
            UrlStatus::New | UrlStatus::Queued | UrlStatus::Assigned
        )
    }
}

impl From<&CrawlResult> for UrlStatus {
    fn from(result: &CrawlResult) -> Self {
        match result {
            CrawlResult::NoDomainError { .. } | CrawlResult::AbortError { .. } => {
                UrlStatus::ErrorOther
            }
            CrawlResult::RobotsUnavailableError { .. } => UrlStatus::ErrorTimeout,
            CrawlResult::RobotsDeniedError { .. } => UrlStatus::ErrorRobotsDenied,
            CrawlResult::CrawlContent { .. } => UrlStatus::Crawled,
            CrawlResult::NoResponseTextError { status, .. }
            | CrawlResult::HtmlParsingError { status, .. } => {
                if status.as_u16() == 404 {
                    UrlStatus::Error404
                } else {
                    UrlStatus::ErrorOther
                }
            }
        }
    }
}

#[derive(Clone, Debug)]
pub struct FoundURL {
    url: Url,
    status: UrlStatus,
    timestamp: chrono::DateTime<Utc>,
    last_crawled: Option<NaiveDate>,
}

#[allow(dead_code)]
impl FoundURL {
    pub fn new(
        url: Url,
        status: UrlStatus,
        timestamp: chrono::DateTime<Utc>,
        last_crawled: Option<NaiveDate>,
    ) -> Self {
        Self {
            url,
            status,
            timestamp,
            last_crawled,
        }
    }

    pub fn url(&self) -> &Url {
        &self.url
    }

    pub fn status(&self) -> &UrlStatus {
        &self.status
    }

    pub fn timestamp(&self) -> DateTime<Utc> {
        self.timestamp
    }

    pub fn last_crawled(&self) -> Option<NaiveDate> {
        self.last_crawled
    }
}

// replace both URLDatabase and DomainLinkDatabase
pub struct BloomDatabase {
    // single bloom and format!("{}::{}") instead of key value
    bloom: AtomicBloomFilter,
    changed: AtomicBool,
    home: PathBuf,
}

impl BloomDatabase {
    pub async fn new(home: &Path) -> anyhow::Result<Arc<Self>> {
        let bloompath = bloom_path(home);
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
            let bloom: AtomicBloomFilter =
                AtomicBloomFilter::with_false_pos(1e-6).expected_items(NUM_DOMAINS_IN_BLOOM_FILTER);
            file.rewind().await?;
            file.write_all(&postcard::to_stdvec(&bloom)?).await?;
            bloom
        };

        let db = Arc::new(BloomDatabase {
            bloom,
            changed: false.into(),
            home: home.to_path_buf(),
        });

        let weak = Arc::downgrade(&db);

        spawn(async move {
            loop {
                sleep(BLOOM_DB_SAVE_INTERVAL).await;
                if let Some(db) = weak.upgrade() {
                    let db = db.clone();

                    if !db.changed.load(std::sync::atomic::Ordering::Relaxed) {
                        continue;
                    }
                    let bloompath = bloom_path(&db.home);

                    if let Ok(mut file) = tokio::fs::File::options()
                        .write(true)
                        .create(false)
                        .open(bloompath)
                        .await
                        && let Ok(vec) = postcard::to_stdvec(&db.bloom)
                    {
                        if file.write_all(&vec).await.is_err() {
                            eprintln!("Cannot write to bloom file");
                            continue;
                        }
                    } else {
                        eprintln!("Cannot open bloom file");
                        continue;
                    }

                    db.changed.store(false, std::sync::atomic::Ordering::SeqCst);
                } else {
                    break;
                }
            }
        });
        Ok(db)
    }

    pub fn get_domain_score(&self, domain: &str) -> f32 {
        DomainGroups::iter()
            .filter(|group| self.bloom.contains(&format!("{group}::{domain}")))
            .map(|group| group.score())
            .sum()
    }

    pub fn update_domain_links<I>(&self, source: &str, target: I)
    where
        I: IntoIterator<Item = String>,
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

        let mut any_change = false;
        for v in target {
            any_change |= !self.bloom.insert(&Self::format_group_links(&group, &v));
        }
        if any_change {
            self.changed
                .store(true, std::sync::atomic::Ordering::SeqCst);
        }
    }

    fn format_group_links(group: &DomainGroups, domain: &str) -> String {
        format!("{group}::{domain}")
    }

    pub fn update_found_urls<'a, I>(&self, found_urls: I)
    where
        I: IntoIterator<Item = &'a mut FoundURL>,
    {
        let times = Self::db_times();
        let mut any_change = false;
        found_urls.into_iter().for_each(|url| {
            if url.status.crawled() {
                url.last_crawled = Some(url.timestamp.date_naive());
            }
            for time in times {
                if self.bloom.contains(&Self::format_url_time(time, &url.url)) {
                    url.last_crawled =
                        Some(max(time, url.last_crawled.unwrap_or(NaiveDate::default())));
                }
            }
            any_change |= !self
                .bloom
                .insert(&Self::format_url_time(times[0], &url.url));
        });
        if any_change {
            self.changed
                .store(true, std::sync::atomic::Ordering::SeqCst);
        }
    }

    fn format_url_time(time: NaiveDate, url: &Url) -> String {
        format!("{}-{}::{}", time.year(), time.month(), url.as_str())
    }

    fn db_times() -> [NaiveDate; 3] {
        let time = Utc::now().date_naive();
        if let Some(first) = time.with_day(1)
            && let Some(second) = first.checked_sub_months(Months::new(1))
            && let Some(third) = second.checked_sub_months(Months::new(1))
        {
            [first, second, third]
        } else {
            [
                NaiveDate::default(),
                NaiveDate::default(),
                NaiveDate::default(),
            ]
        }
    }
}

impl Drop for BloomDatabase {
    fn drop(&mut self) {
        if !self.changed.load(std::sync::atomic::Ordering::SeqCst) {
            return;
        }
        let bloompath = bloom_path(&self.home);

        if let Ok(mut file) = fs::File::options()
            .write(true)
            .create(false)
            .open(bloompath)
            && let Ok(vec) = postcard::to_stdvec(&self.bloom)
        {
            if file.write_all(&vec).is_err() {
                eprintln!("Cannot write to bloom file (final)");
            }
        } else {
            eprintln!("Cannot open bloom file");
        }
    }
}

#[cfg(test)]
mod tests {
    use chrono::{Datelike, Utc};
    use reqwest::Url;

    use super::{BloomDatabase, DomainGroups, FoundURL, UrlStatus};
    use crate::test_support::scratch_home;

    fn found(url: &Url, status: UrlStatus) -> FoundURL {
        FoundURL::new(url.clone(), status, Utc::now(), None)
    }

    #[tokio::test]
    async fn a_crawled_url_is_remembered_across_restarts() {
        let home = scratch_home("bloom");
        let url = Url::parse("https://example.com/page").unwrap();
        let today = Utc::now().date_naive();

        let db = BloomDatabase::new(&home).await.unwrap();
        let mut new = [found(&url, UrlStatus::New)];
        db.update_found_urls(new.iter_mut());
        assert_eq!(new[0].last_crawled(), None);

        let mut crawled = [found(&url, UrlStatus::Crawled)];
        db.update_found_urls(crawled.iter_mut());
        assert_eq!(crawled[0].last_crawled(), Some(today));
        drop(db);

        // Seen again after a restart, the URL is known to have been crawled this month.
        let db = BloomDatabase::new(&home).await.unwrap();
        let mut again = [found(&url, UrlStatus::New)];
        db.update_found_urls(again.iter_mut());
        let first_of_month = today.with_day(1).unwrap();
        assert_eq!(again[0].last_crawled(), Some(first_of_month));
    }

    #[tokio::test]
    async fn links_from_a_trusted_domain_raise_the_score_of_the_domains_they_link_to() {
        let home = scratch_home("domain-links");
        let db = BloomDatabase::new(&home).await.unwrap();
        assert!(db.get_domain_score("linked.example").abs() < f32::EPSILON);

        db.update_domain_links("en.wikipedia.org", ["linked.example".to_owned()]);
        db.update_domain_links("unknown-source.example", ["ignored.example".to_owned()]);

        assert!(
            (db.get_domain_score("linked.example") - DomainGroups::WikipediaEn.score()).abs()
                < f32::EPSILON
        );
        assert!(db.get_domain_score("ignored.example").abs() < f32::EPSILON);
    }

    #[test]
    fn only_finished_statuses_count_as_crawled() {
        assert!(!UrlStatus::New.crawled());
        assert!(!UrlStatus::Queued.crawled());
        assert!(UrlStatus::Error404.crawled());
        assert!(UrlStatus::Crawled.crawled());
    }
}
