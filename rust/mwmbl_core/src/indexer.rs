use std::{
    borrow::Cow,
    cmp::max,
    collections::{HashMap, HashSet},
    fs::{self, File},
    io::{Read, Write},
    os::fd::AsRawFd,
    path::Path,
    str::FromStr,
    sync::Arc,
    time::Instant,
};

use anyhow::{Context, bail};
use file_guard::Lock;
use futures_util::future::join_all;
use itertools::{Either, Itertools};
use rayon::iter::{IntoParallelIterator, ParallelIterator};
use reqwest::Url;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::{
    runtime::Handle,
    sync::{OwnedRwLockReadGuard, OwnedRwLockWriteGuard, RwLock},
    task::spawn_blocking,
};

use crate::{
    agreement::Agreement,
    blacklist_providers::BlacklistProvider,
    constants::{INDEX_VERSION, ITEM_FACTORY, METADATA_CONSTANT, METADATA_SIZE, PAGE_SIZE},
    index::{PageIoError, pack_page, read_index_page, tokenize_document, write_index_page},
    rank::{HeuristicRanker, Ranker},
    tokenizer::clean_unicode,
};

/// The state of the document in the index. A value of None indicates an organic search result
#[derive(Clone, Serialize, Deserialize, Debug)]
pub enum DocumentState {
    SyncedWithMainIndex = -2,
    DELETED = -1,
    FromUser = 2,
    FromGoogle = 3,
    FromWiki = 4,
    OrganicApproved = 7,
    FromUserApproved = 8,
    FromGoogleApproved = 9,
    FromWikiApproved = 10,
}

#[derive(Clone, Serialize, Deserialize, Debug)]
pub enum DocumentSource {
    Wikipedia = 1,
    Staan = 2,
    StaanIndexed = 3,
    Widget = 4,
    CommonsPublicDomain = 100,
    CommonsCc0 = 101,
    CommonsCcBy2_0 = 102,
    CommonsCcBy2_5 = 103,
    CommonsCcBy3_0 = 104,
    CommonsCcBy4_0 = 105,
    CommonsCcBySa2_0 = 106,
    CommonsCcBySa2_5 = 107,
    CommonsCcBySa3_0 = 108,
    CommonsCcBySa4_0 = 109,
    CommonsOther = 110,
}

#[derive(Clone, Serialize, Deserialize, Debug, Default)]
pub struct Document {
    title: String,
    url: String,
    extract: String,
    score: Option<f32>,
    term: Option<String>,
    state: Option<DocumentState>,
    user_ids: Option<Vec<i32>>,
    last_crawled: Option<i64>,
    source: Option<DocumentSource>,
}

#[derive(Debug, Clone)]
pub enum IndexMode {
    READ,
    WRITE,
}

impl Document {
    #[allow(clippy::too_many_arguments)]
    #[must_use]
    pub fn new(
        title: String,
        url: String,
        extract: String,
        score: Option<f32>,
        term: Option<String>,
        state: Option<DocumentState>,
        user_ids: Option<Vec<i32>>,
        last_crawled: Option<i64>,
        source: Option<DocumentSource>,
    ) -> Self {
        Self {
            title,
            url,
            extract,
            score,
            term,
            state,
            user_ids,
            last_crawled,
            source,
        }
    }

    #[must_use]
    pub fn title(&self) -> &str {
        &self.title
    }

    #[must_use]
    pub fn url(&self) -> &str {
        &self.url
    }

    #[must_use]
    pub fn extract(&self) -> &str {
        &self.extract
    }

    #[must_use]
    pub fn score(&self) -> Option<f32> {
        self.score
    }

    #[must_use]
    pub fn term(&self) -> Option<&String> {
        self.term.as_ref()
    }

    #[must_use]
    pub fn state(&self) -> Option<&DocumentState> {
        self.state.as_ref()
    }

    #[must_use]
    pub fn user_ids(&self) -> Option<&Vec<i32>> {
        self.user_ids.as_ref()
    }

    #[must_use]
    pub fn last_crawled(&self) -> Option<i64> {
        self.last_crawled
    }

    #[must_use]
    pub fn source(&self) -> Option<&DocumentSource> {
        self.source.as_ref()
    }
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct TinyIndexMetadata {
    version: i32,
    page_size: usize,
    num_pages: usize,
    item_factory: String,
}

impl TinyIndexMetadata {
    fn new(page_size: usize, num_pages: usize) -> TinyIndexMetadata {
        TinyIndexMetadata {
            version: INDEX_VERSION,
            page_size,
            num_pages,
            item_factory: ITEM_FACTORY.to_owned(),
        }
    }
    fn to_bytes(&self) -> Result<[u8; METADATA_SIZE!()], PageIoError> {
        let mut bytes: [u8; METADATA_SIZE!()] = [0; METADATA_SIZE!()];
        if let Some(slice) = bytes.get_mut(..METADATA_CONSTANT.len()) {
            slice.copy_from_slice(METADATA_CONSTANT);
        } else {
            return Err(PageIoError::Unreadable(format!(
                "METADATA_CONSTANT is too big for page size ({})",
                METADATA_SIZE!()
            )));
        }
        let json = serde_json::to_string(self).map_err(|err| PageIoError::Io(err.into()))?;

        if let Some(slice) =
            bytes.get_mut(METADATA_CONSTANT.len()..METADATA_CONSTANT.len() + json.len())
        {
            slice.copy_from_slice(json.as_bytes());
        } else {
            return Err(PageIoError::Unreadable(format!(
                "Metadata is too big for page size ({})",
                METADATA_SIZE!()
            )));
        }
        Ok(bytes)
    }
    fn from_bytes(bytes: &[u8]) -> Result<TinyIndexMetadata, PageIoError> {
        if !bytes.starts_with(METADATA_CONSTANT) {
            return Err(PageIoError::Unreadable(
                "This doesn't seem to be an index file".to_owned(),
            ));
        }
        if let Some(slice) = bytes.get(METADATA_CONSTANT.len()..) {
            let json: Vec<u8> = slice.iter().copied().take_while(|e| *e != 0).collect();
            serde_json::from_slice(json.as_slice()).map_err(|err| PageIoError::Io(err.into()))
        } else {
            Err(PageIoError::Unreadable(
                "Not enought bytes for TinyIndexMetadata".to_owned(),
            ))
        }
    }
}

struct ReadPageLock {
    process_lock: file_guard::FileGuard<Arc<File>>,
    _task_lock: OwnedRwLockReadGuard<()>,
}

struct WritePageLock {
    process_lock: file_guard::FileGuard<Arc<File>>,
    _task_lock: OwnedRwLockWriteGuard<()>,
}

#[derive(Debug, Clone)]
pub struct TinyIndex {
    index_file: Arc<File>,
    mode: IndexMode,
    metadata: Arc<TinyIndexMetadata>,
    page_locks: Arc<Vec<Arc<RwLock<()>>>>,
}

impl TinyIndex {
    pub fn from_file(path: &Path, mode: IndexMode) -> anyhow::Result<TinyIndex> {
        let mut file = File::open(path)?;
        let mut metadata_bytes: [u8; 4096] = [0; METADATA_SIZE!()];
        file.read_exact(&mut metadata_bytes)?;

        let metadata = TinyIndexMetadata::from_bytes(&metadata_bytes)?;
        if metadata.item_factory != ITEM_FACTORY {
            bail!(
                "Metadata item factory '{}' in the index does not match the passed item factory: '{}",
                metadata.item_factory,
                ITEM_FACTORY
            );
        }
        let file = match mode {
            IndexMode::READ => File::options()
                .read(true)
                .write(false)
                .create(false)
                .truncate(false)
                .open(path),
            IndexMode::WRITE => File::options()
                .read(true)
                .write(true)
                .create(false)
                .truncate(false)
                .open(path),
        }?;

        let mut page_locks = Vec::new();
        for _ in 0..metadata.num_pages {
            page_locks.push(Arc::new(RwLock::new(())));
        }

        Ok(Self {
            index_file: Arc::new(file),
            mode,
            metadata: Arc::new(metadata),
            page_locks: Arc::new(page_locks),
        })
    }
    fn create(path: &Path, num_pages: usize, overwrite: bool) -> anyhow::Result<TinyIndex> {
        if path.is_file() && !overwrite {
            bail!("Index file '{}' already exists", path.display());
        }
        let metadata = TinyIndexMetadata::new(METADATA_SIZE!(), num_pages);
        let bytes = metadata.to_bytes()?;

        let page_bytes = pack_page::<Value>(PAGE_SIZE!(), &[])?;

        let mut file = File::create(path)?;
        file.write_all(&bytes)?;
        for _ in 0..num_pages {
            file.write_all(&page_bytes.0)?;
        }
        file.flush()?;
        drop(file);
        Self::from_file(path, IndexMode::READ)
    }
    fn validate(&self, num_pages: usize, page_size: usize) -> anyhow::Result<()> {
        if self.metadata.num_pages != num_pages || self.metadata.page_size != page_size {
            bail!(
                "Existing index page sizes ({}) or number of pages ({}) do not match",
                self.metadata.num_pages,
                self.metadata.page_size
            );
        }
        Ok(())
    }

    async fn lock_page_read_only(&self, page: usize) -> anyhow::Result<ReadPageLock> {
        let start: usize = page * self.metadata.page_size + METADATA_SIZE!();
        let process_lock = file_guard::lock(
            self.index_file.clone(),
            Lock::Shared,
            start,
            self.metadata.page_size,
        )?;
        let Some(task_lock) = self.page_locks.get(page) else {
            bail!("Invalid page number");
        };
        let task_lock = task_lock.clone().read_owned().await;
        Ok(ReadPageLock {
            _task_lock: task_lock,
            process_lock,
        })
    }

    async fn lock_page_write(&self, page: usize) -> anyhow::Result<WritePageLock> {
        if matches!(self.mode, IndexMode::READ) {
            bail!("Index is in read only mode");
        }
        let start: usize = page * self.metadata.page_size + METADATA_SIZE!();
        let process_lock = file_guard::lock(
            self.index_file.clone(),
            Lock::Exclusive,
            start,
            self.metadata.page_size,
        )?;
        let Some(task_lock) = self.page_locks.get(page) else {
            bail!("Invalid page number");
        };
        let task_lock = task_lock.clone().write_owned().await;
        Ok(WritePageLock {
            _task_lock: task_lock,
            process_lock,
        })
    }

    fn get_page<T: for<'a> Deserialize<'a>>(
        lock: Either<&ReadPageLock, &WritePageLock>,
        term: Option<&String>,
    ) -> anyhow::Result<Vec<T>> {
        let process_lock = lock.either(|r| &r.process_lock, |w| &w.process_lock);
        Ok(read_index_page(
            process_lock.as_raw_fd(),
            process_lock.offset() as u64,
            process_lock.len(),
            term,
        )?)
    }

    pub async fn retrieve<T: for<'a> Deserialize<'a>>(&self, term: &str) -> anyhow::Result<Vec<T>> {
        let page = self.get_key_page_index(term);
        let lock = self.lock_page_read_only(page).await?;
        Self::get_page(Either::Left(&lock), Some(&term.to_owned()))
    }

    fn store_in_page<T: Serialize>(lock: &WritePageLock, values: &[T]) -> anyhow::Result<usize> {
        Ok(write_index_page(
            lock.process_lock.as_raw_fd(),
            lock.process_lock.offset() as u64,
            lock.process_lock.len(),
            values,
        )?)
    }

    #[must_use]
    pub fn get_key_page_index(&self, key: &str) -> usize {
        murmurhash32::murmurhash3_with_seed::<0>(key.as_bytes()) as usize % self.metadata.num_pages
    }

    /// Merges each page's new documents into it and writes it back. Each page has its own
    /// lock and is read and written independently, so the pages are done in parallel.
    async fn store_pages(
        &self,
        page_documents: HashMap<usize, Vec<Document>>,
        mark_synced: bool,
    ) -> Vec<StoredPage> {
        let stored: Vec<_> = join_all(
            page_documents
                .into_par_iter()
                .map(|(page, docs)| (page, docs, self.clone()))
                .map(|(page, docs, index)| async move {
                    spawn_blocking(move || {
                        Handle::current().block_on(async move {
                            index.store_page(page, &docs, mark_synced).await
                        })
                    })
                    .await
                })
                .collect::<Vec<_>>(),
        )
        .await;
        stored.into_iter().flatten_ok().flatten().collect()
    }

    async fn store_page(
        &self,
        page: usize,
        docs: &[Document],
        mark_synced: bool,
    ) -> Option<StoredPage> {
        let Ok(lock) = self.lock_page_write(page).await else {
            eprintln!("Skipping index page {page}");
            return None;
        };
        let Ok(existing_documents) = TinyIndex::get_page(Either::Right(&lock), None) else {
            eprintln!("Skipping index page {page}");
            return None;
        };
        let stored_copy = find_stored_copies(docs, &existing_documents);
        let mut combined_documents = combine_documents(
            &existing_documents,
            docs,
            mark_synced,
            &HeuristicRanker::new(f32::NEG_INFINITY),
        );
        let Ok(num_stored) = TinyIndex::store_in_page(&lock, &combined_documents) else {
            eprintln!("Skipping index page {page}");
            return None;
        };
        combined_documents.truncate(num_stored);
        Some(StoredPage {
            stored: combined_documents,
            existing: existing_documents,
            stored_copies: stored_copy,
        })
    }

    pub async fn index_documents<I, T: BlacklistProvider>(
        &self,
        documents: I,
        blacklist: &T,
    ) -> anyhow::Result<IndexedDocuments>
    where
        I: IntoIterator<Item = Document>,
    {
        if matches!(self.mode, IndexMode::READ) {
            bail!("index_documents can only be done with IndexMode::WRITE");
        }
        let mut page_documents = Vec::new();
        let mut submitted_by_url: HashMap<String, Document> = HashMap::new();

        for mut doc in documents {
            if let Ok(url) = Url::from_str(&doc.url)
                && let Some(domain) = url.domain()
                && !blacklist.is_domain_blacklisted(domain).await
            {
                doc.title = clean_unicode(&doc.title);
                doc.extract = clean_unicode(&doc.extract);

                let tokens = tokenize_document(&doc.url, &doc.title, &doc.extract);
                for token in tokens {
                    let page = self.get_key_page_index(&token);
                    let mut doc = doc.clone();
                    doc.term = Some(token);
                    page_documents.push((page, doc));
                }
                submitted_by_url.insert(doc.url().to_owned(), doc.clone());
            }
        }
        let page_documents = page_documents.into_iter().into_group_map();

        let mut new_page_doc_counts: HashMap<String, usize> = HashMap::new();
        let mut stored_urls: HashSet<String> = HashSet::new();
        let mut held_urls: HashSet<String> = HashSet::new();
        let mut copies: HashMap<String, Vec<Document>> = HashMap::new();

        for page in self.store_pages(page_documents, false).await {
            for stored in page.stored {
                if !matches!(stored.state, Some(DocumentState::SyncedWithMainIndex))
                    && let Some(term) = stored.term()
                {
                    *new_page_doc_counts.entry(term.to_owned()).or_default() += 1;
                }
                stored_urls.insert(stored.url().to_owned());
            }
            for held in page.existing {
                held_urls.insert(held.url().to_owned());
            }
            for copy in page.stored_copies {
                copies.entry(copy.url().to_owned()).or_default().push(copy);
            }
        }
        let agreements: Vec<Agreement> = copies
            .into_values()
            .filter_map(|docs| {
                let Some(((copy_title, copy_url, copy_extract, Some(copy_user_id)), _)) = docs
                    .iter()
                    .map(|doc| {
                        (
                            (
                                doc.title(),
                                doc.url(),
                                doc.extract(),
                                doc.user_ids().and_then(|v| v.last()),
                            ),
                            (),
                        )
                    })
                    .into_group_map()
                    .into_iter()
                    .max_by_key(|(_, v)| v.len())
                else {
                    return None;
                };
                let last_crawled = docs
                    .iter()
                    .map(|doc| doc.last_crawled)
                    .max()
                    .unwrap_or(None);
                let document = submitted_by_url.get(copy_url)?;
                Some(Agreement::new(
                    document,
                    copy_title,
                    copy_url,
                    copy_extract,
                    *copy_user_id,
                    last_crawled,
                ))
            })
            .collect();

        let incoming_urls: HashSet<String> = submitted_by_url.into_keys().collect();
        let written_urls: HashSet<String> =
            stored_urls.intersection(&incoming_urls).cloned().collect();
        let new_urls: HashSet<String> = written_urls.difference(&held_urls).cloned().collect();

        Ok(IndexedDocuments {
            end_time: Instant::now(),
            new_page_doc_counts,
            agreements,
            new_urls,
            stored_urls: written_urls,
        })
    }

    pub async fn index_pages(
        &self,
        page_documents: HashMap<usize, Vec<Document>>,
        mark_synced: bool,
    ) -> anyhow::Result<HashMap<String, usize>> {
        let mut new_page_doc_counts: HashMap<String, usize> = HashMap::new();

        for page in self.store_pages(page_documents, mark_synced).await {
            for stored in page.stored {
                if !matches!(stored.state, Some(DocumentState::SyncedWithMainIndex))
                    && let Some(term) = stored.term()
                {
                    *new_page_doc_counts.entry(term.to_owned()).or_default() += 1;
                }
            }
        }
        Ok(new_page_doc_counts)
    }
}

pub fn create_index(
    index_path: &Path,
    num_pages: usize,
    rebuild_on_mismatch: bool,
) -> anyhow::Result<()> {
    fs::create_dir_all(index_path.parent().context("Invalid index path")?)?;

    if !index_path.exists() {
        println!("======================================");
        println!("Index not found - creating a new index");
        println!("======================================");
        TinyIndex::create(index_path, num_pages, true)?;
        return Ok(());
    }

    match TinyIndex::from_file(index_path, IndexMode::READ) {
        Ok(index) => {
            println!("======================================");
            println!("Found existing index at {}", index_path.display());
            println!("======================================");
            let validation = index.validate(num_pages, PAGE_SIZE!());
            if !rebuild_on_mismatch {
                validation?;
                return Ok(());
            }
            println!("rebuilding index");
            TinyIndex::create(index_path, num_pages, true)?;
        }
        Err(err) => {
            if !rebuild_on_mismatch {
                return Err(err);
            }
            println!("Invalid index - rebuilding {}", index_path.display());
            TinyIndex::create(index_path, num_pages, true)?;
        }
    }

    Ok(())
}

pub struct IndexedDocuments {
    end_time: Instant,
    new_page_doc_counts: HashMap<String, usize>,
    agreements: Vec<Agreement>,
    new_urls: HashSet<String>,
    stored_urls: HashSet<String>,
}

impl IndexedDocuments {
    #[must_use]
    pub fn end_time(&self) -> Instant {
        self.end_time
    }

    #[must_use]
    pub fn new_page_doc_counts(&self) -> &HashMap<String, usize> {
        &self.new_page_doc_counts
    }

    #[must_use]
    pub fn agreements(&self) -> &[Agreement] {
        &self.agreements
    }

    #[must_use]
    pub fn new_urls(&self) -> &HashSet<String> {
        &self.new_urls
    }

    #[must_use]
    pub fn stored_urls(&self) -> &HashSet<String> {
        &self.stored_urls
    }
}

#[derive(Default)]
pub struct StoredPage {
    stored: Vec<Document>,
    existing: Vec<Document>,
    stored_copies: Vec<Document>,
}

fn find_stored_copies<'b, 'a, I, J>(incoming: I, existing: J) -> Vec<Document>
where
    I: IntoIterator<Item = &'b Document>,
    J: IntoIterator<Item = &'a Document>,
{
    let submitters_by_url = incoming
        .into_iter()
        .filter_map(|v| v.user_ids.clone().map(|user_ids| (v.url.clone(), user_ids)))
        .flat_map(|(url, user_ids)| user_ids.into_iter().map(move |id| (url.clone(), id)))
        .collect::<HashSet<_>>();
    let mut stored_copies = Vec::new();
    for document in existing {
        let attributed: bool = !document
            .state
            .as_ref()
            .is_some_and(DocumentState::is_curated)
            && document.source.is_none();
        if attributed
            && let Some(author_ids) = &document.user_ids
            && let Some(author_id) = author_ids.last()
            && submitters_by_url.contains(&(document.url.clone(), *author_id))
        {
            stored_copies.push(document.clone());
        }
    }
    stored_copies
}

impl DocumentState {
    #[must_use]
    pub fn is_curated(&self) -> bool {
        matches!(
            self,
            DocumentState::OrganicApproved
                | DocumentState::FromUserApproved
                | DocumentState::FromGoogleApproved
                | DocumentState::FromWikiApproved
        )
    }
}

/// Ranks and deduplicates a page's documents. Everything up to the end works on borrowed
/// documents, so each document that is kept is cloned once, and only a document merged
/// from several copies is built anew.
fn combine_documents<'a, I, J, R: Ranker>(
    existing_documents: J,
    documents: I,
    mark_synced: bool,
    ranker: &R,
) -> Vec<Document>
where
    I: IntoIterator<Item = &'a Document>,
    J: IntoIterator<Item = &'a Document>,
{
    let sorted = sort_documents(existing_documents, documents, ranker);
    let mut values: Vec<(usize, Cow<'a, Document>)> = sorted
        .into_iter()
        .enumerate()
        .map(|(order, doc)| (doc.url.as_str(), (order, Cow::Borrowed(doc))))
        .into_group_map()
        .into_values()
        .filter_map(merge_user_ids_last_crawled)
        .map(|(order, doc)| (doc.title.clone(), (order, doc)))
        .into_group_map()
        .into_values()
        .filter_map(merge_user_ids_last_crawled)
        .collect();
    values.sort_by_key(|v| v.0);
    values
        .into_iter()
        .map(|(_, doc)| {
            let mut doc = doc.into_owned();
            if mark_synced {
                doc.state = Some(DocumentState::SyncedWithMainIndex);
            }
            doc
        })
        .collect()
}

/// The first document, with the history of all of them: the latest crawl, and every
/// user id, latest crawl last. A document with no other copies is returned as it is.
fn merge_user_ids_last_crawled(
    mut docs: Vec<(usize, Cow<'_, Document>)>,
) -> Option<(usize, Cow<'_, Document>)> {
    if docs.len() <= 1 {
        return docs.pop();
    }
    let (idx, mut best_doc) = docs
        .first()
        .map(|(idx, doc)| (*idx, Document::clone(doc)))?;
    docs.sort_by_key(|(_, doc)| doc.last_crawled.unwrap_or(i64::MIN));
    let mut user_ids: Vec<i32> = Vec::new();
    for (_, doc) in &docs {
        best_doc.last_crawled = max(best_doc.last_crawled, doc.last_crawled);
        for &id in doc.user_ids.iter().flatten() {
            user_ids.retain(|v| *v != id);
            user_ids.push(id);
        }
    }
    best_doc.user_ids = Some(user_ids);
    Some((idx, Cow::Owned(best_doc)))
}

fn sort_documents<'a, I, J, R: Ranker>(
    existing_documents: J,
    documents: I,
    ranker: &R,
) -> Vec<&'a Document>
where
    I: IntoIterator<Item = &'a Document>,
    J: IntoIterator<Item = &'a Document>,
{
    let mut curated = Vec::new();
    let mut widgets = Vec::new();
    let mut widgets_urls = HashSet::new();
    let mut existing_other = Vec::new();

    for doc in existing_documents {
        if doc.state.as_ref().is_some_and(DocumentState::is_curated) {
            curated.push(doc);
        } else if matches!(&doc.source, Some(DocumentSource::Widget)) {
            widgets_urls.insert(doc.url.clone());
            widgets.push(doc);
        } else {
            existing_other.push(doc);
        }
    }

    let mut other_documents = Vec::new();

    for doc in documents {
        if matches!(doc.source, Some(DocumentSource::Widget)) {
            if !widgets_urls.contains(&doc.url) {
                widgets.push(doc);
            }
        } else {
            other_documents.push(doc);
        }
    }

    let mut sorted_documents: Vec<_> = other_documents
        .into_iter()
        .chain(existing_other)
        .filter_map(|doc| Some((doc.term.as_deref()?, doc)))
        .into_group_map()
        .into_iter()
        .flat_map(|(term, docs)| {
            ranker
                .order_results(&term.split_whitespace().collect::<Vec<&str>>(), &docs, true)
                .into_iter()
                .enumerate()
        })
        .collect();
    sorted_documents.sort_by_key(|v| v.0);

    curated
        .into_iter()
        .chain(widgets)
        .chain(sorted_documents.into_iter().map(|v| v.1))
        .collect()
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::indexing_slicing)]
mod tests {
    use super::{Document, DocumentState, combine_documents};
    use crate::rank::HeuristicRanker;

    fn document(title: &str, url: &str, last_crawled: i64, user_id: i32) -> Document {
        Document::new(
            title.to_owned(),
            url.to_owned(),
            "rust is a language".to_owned(),
            None,
            Some("rust".to_owned()),
            None,
            Some(vec![user_id]),
            Some(last_crawled),
            None,
        )
    }

    #[test]
    fn copies_of_a_url_merge_their_history_and_a_title_is_kept_once() {
        let existing = [
            document("Rust", "https://rust-lang.org/", 10, 1),
            document("Other", "https://other.example/rust", 5, 4),
        ];
        let incoming = [
            document("Rust", "https://rust-lang.org/", 20, 2),
            document("Rust", "https://copy.example/rust", 30, 3),
        ];

        let combined = combine_documents(
            &existing,
            &incoming,
            true,
            &HeuristicRanker::new(f32::NEG_INFINITY),
        );

        let urls: Vec<&str> = combined.iter().map(Document::url).collect();
        assert_eq!(urls.len(), 2, "{urls:?}");
        assert!(urls.contains(&"https://other.example/rust"), "{urls:?}");
        // Which URL keeps the title depends on hash order, but either way it carries the
        // history of all three copies, oldest crawl first.
        let rust = combined.iter().find(|doc| doc.title() == "Rust").unwrap();
        assert_eq!(rust.last_crawled(), Some(30));
        assert_eq!(rust.user_ids(), Some(&vec![1, 2, 3]));
        assert!(
            combined
                .iter()
                .all(|doc| matches!(doc.state(), Some(DocumentState::SyncedWithMainIndex)))
        );
        let other = combined.iter().find(|doc| doc.title() == "Other").unwrap();
        assert_eq!(
            (other.last_crawled(), other.user_ids()),
            (Some(5), Some(&vec![4]))
        );
    }
}
