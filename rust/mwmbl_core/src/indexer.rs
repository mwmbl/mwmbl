use std::{
    cmp::max,
    collections::{HashMap, HashSet},
    fs::{self, File, TryLockError},
    io::{Read, Write},
    path::Path,
    str::FromStr,
    sync::Arc,
    time::Instant,
};

use anyhow::{Context, bail};
use futures_util::future::join_all;
use itertools::Itertools;
use reqwest::Url;
use serde::{Deserialize, Serialize, Serializer, ser::SerializeSeq};
use serde_json::Value;
use tokio::sync::oneshot;

use crate::{
    agreement::Agreement,
    blacklist_providers::BlacklistProvider,
    constants::{INDEX_VERSION, ITEM_FACTORY, METADATA_CONSTANT, METADATA_SIZE, PAGE_SIZE},
    index::{PageIoError, pack_page, tokenize_document},
    partition::{PageRequest, Partitions},
    rank::Ranker,
    tokenizer::clean_unicode,
};

/// Defines an enum the API and the index pages carry as its integer value, as the Python
/// side writes it. Serde's default for an enum is its variant name, which the server
/// neither sends nor accepts.
macro_rules! integer_enum {
    ($(#[$meta:meta])* $name:ident { $($variant:ident = $value:literal,)+ }) => {
        $(#[$meta])*
        #[derive(Clone, Serialize, Deserialize, Debug, PartialEq, Eq)]
        #[serde(try_from = "i64", into = "i64")]
        pub enum $name {
            $($variant = $value,)+
        }

        impl From<$name> for i64 {
            fn from(value: $name) -> i64 {
                value as i64
            }
        }

        impl TryFrom<i64> for $name {
            type Error = String;

            fn try_from(value: i64) -> Result<Self, Self::Error> {
                match value {
                    $($value => Ok($name::$variant),)+
                    _ => Err(format!("{value} is not a {}", stringify!($name))),
                }
            }
        }
    };
}

/// A state or source as the integer a page holds, kept as it is when this build doesn't know
/// it, as an older build sees one added since. It reads as None from `Document`'s accessors,
/// and is written back unchanged, so a merge never erases a newer state or source.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Coded<T> {
    Known(T),
    Unknown(i64),
}

impl<T> Coded<T> {
    fn known(&self) -> Option<&T> {
        match self {
            Coded::Known(value) => Some(value),
            Coded::Unknown(_) => None,
        }
    }
}

impl<T: Clone + Into<i64>> Serialize for Coded<T> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let value = match self {
            Coded::Known(known) => known.clone().into(),
            Coded::Unknown(value) => *value,
        };
        serializer.serialize_i64(value)
    }
}

impl<'de, T: TryFrom<i64>> Deserialize<'de> for Coded<T> {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let value = i64::deserialize(deserializer)?;
        Ok(T::try_from(value).map_or(Coded::Unknown(value), Coded::Known))
    }
}

integer_enum!(
    /// The state of the document in the index. A value of None indicates an organic search result
    DocumentState {
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
);

integer_enum!(DocumentSource {
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
});

/// `default` on the optional fields because Python writes a document to a page as a tuple
/// with its trailing Nones cut off, so a page item has anywhere from three to nine elements.
#[derive(Clone, Serialize, Deserialize, Debug, Default)]
pub struct Document {
    title: String,
    url: String,
    extract: String,
    #[serde(default)]
    score: Option<f32>,
    #[serde(default)]
    term: Option<String>,
    #[serde(default)]
    state: Option<Coded<DocumentState>>,
    #[serde(default)]
    user_ids: Option<Vec<i32>>,
    #[serde(default)]
    last_crawled: Option<i64>,
    #[serde(default)]
    source: Option<Coded<DocumentSource>>,
    /// The elements a page item from a newer build has after the nine this one knows, kept
    /// to be written back unchanged. The API carries them as `extra`, so a client's
    /// conditional write of a page it read keeps them too.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    extra: Vec<Value>,
}

#[derive(Debug, Clone)]
pub enum IndexMode {
    READ,
    WRITE,
}

/// What opening an index for writing does when another process holds its lock.
#[derive(Debug, Clone, Copy)]
pub enum LockPolicy {
    /// Fail at once, as the crawler does: two crawlers must not share an index.
    Fail,
    /// Accept requests at once and apply them once the lock is taken, as the index server
    /// does while the container it replaces finishes its writes.
    Wait,
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
            state: state.map(Coded::Known),
            user_ids,
            last_crawled,
            source: source.map(Coded::Known),
            extra: Vec::new(),
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
        self.state.as_ref().and_then(Coded::known)
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
        self.source.as_ref().and_then(Coded::known)
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

/// A handle on an index file. Each page belongs to one of the index's owner threads, which
/// does every read and write of it, so pages need no locks. Clones share the threads, which
/// stop when the last clone is dropped.
#[derive(Clone)]
pub struct TinyIndex {
    mode: IndexMode,
    metadata: Arc<TinyIndexMetadata>,
    partitions: Arc<Partitions>,
}

impl TinyIndex {
    /// Opens the index at `path`, with `num_threads` owner threads for its pages.
    ///
    /// In `WRITE` mode it takes an exclusive `flock` on the whole file, held while any clone
    /// is alive, so only one process ever writes the index.
    ///
    /// # Errors
    ///
    /// Fails if the file can't be read, isn't an index, `num_threads` is zero, or, in `WRITE`
    /// mode, another process has the index open for writing.
    pub fn from_file(
        path: &Path,
        mode: IndexMode,
        num_threads: usize,
    ) -> anyhow::Result<TinyIndex> {
        Self::open(path, mode, num_threads, LockPolicy::Fail)
    }

    /// Opens the index at `path` for writing, as `from_file` does in `WRITE` mode, but with
    /// `policy` deciding what happens while another process holds its lock. With `Wait`,
    /// requests queue until the owner threads have taken it.
    ///
    /// # Errors
    ///
    /// Fails as `from_file` does.
    pub fn open_writer(
        path: &Path,
        num_threads: usize,
        policy: LockPolicy,
    ) -> anyhow::Result<TinyIndex> {
        Self::open(path, IndexMode::WRITE, num_threads, policy)
    }

    fn open(
        path: &Path,
        mode: IndexMode,
        num_threads: usize,
        policy: LockPolicy,
    ) -> anyhow::Result<TinyIndex> {
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
        let file = File::options()
            .read(true)
            .write(matches!(mode, IndexMode::WRITE))
            .open(path)?;
        let wait_for_lock = matches!(mode, IndexMode::WRITE) && matches!(policy, LockPolicy::Wait);
        if matches!(mode, IndexMode::WRITE) && matches!(policy, LockPolicy::Fail) {
            match file.try_lock() {
                Ok(()) => {}
                Err(TryLockError::WouldBlock) => bail!(
                    "Index file '{}' is already open for writing by another process",
                    path.display()
                ),
                Err(TryLockError::Error(error)) => {
                    return Err(error).context("Could not lock the index file");
                }
            }
        }
        let partitions = Partitions::start(
            &Arc::new(file),
            metadata.page_size,
            metadata.num_pages,
            num_threads,
            wait_for_lock,
        )?;

        Ok(Self {
            mode,
            metadata: Arc::new(metadata),
            partitions: Arc::new(partitions),
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
        Self::from_file(path, IndexMode::READ, 1)
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

    /// The documents on `term`'s page that belong to it.
    ///
    /// # Errors
    ///
    /// Fails if the page can't be read or doesn't decode, or its owner thread has stopped.
    pub async fn retrieve(&self, term: &str) -> anyhow::Result<Vec<Document>> {
        let page = self.get_key_page_index(term);
        let (reply, receiver) = oneshot::channel();
        self.partitions.send(PageRequest::Retrieve {
            page,
            term: term.to_owned(),
            reply,
        })?;
        let documents = receiver.await.context("The index owner thread stopped")??;
        Ok(documents)
    }

    /// The documents on page `page`, and its version for `write_page_if`. A page that
    /// doesn't decode reads as empty, so a write against its version resets it.
    ///
    /// # Errors
    ///
    /// Fails if the page isn't in the index, can't be read, or its owner thread has stopped.
    pub async fn read_page(&self, page: usize) -> anyhow::Result<VersionedPage> {
        let (reply, receiver) = oneshot::channel();
        self.partitions
            .send(PageRequest::ReadPage { page, reply })?;
        let page = receiver.await.context("The index owner thread stopped")??;
        Ok(page)
    }

    /// Writes as many of `documents` as fit over page `page`, best first, if it is still at
    /// `if_version`. Otherwise nothing is written and it returns `Conflict`.
    ///
    /// # Errors
    ///
    /// Fails if the index is read only, the page isn't in it, can't be read or written, or
    /// the documents won't pack, or if its owner thread has stopped.
    pub async fn write_page_if(
        &self,
        page: usize,
        documents: Vec<Document>,
        if_version: u64,
    ) -> anyhow::Result<ConditionalWrite> {
        if matches!(self.mode, IndexMode::READ) {
            bail!("Index is in read only mode");
        }
        let (reply, receiver) = oneshot::channel();
        self.partitions.send(PageRequest::WritePage {
            page,
            docs: documents,
            if_version,
            reply,
        })?;
        let written = receiver.await.context("The index owner thread stopped")??;
        Ok(written)
    }

    #[must_use]
    pub fn num_pages(&self) -> usize {
        self.metadata.num_pages
    }

    /// Whether this handle's owner threads may touch the index's pages: always, unless it
    /// was opened with `LockPolicy::Wait` and the lock isn't held yet.
    #[must_use]
    pub fn holds_lock(&self) -> bool {
        self.partitions.holds_lock()
    }

    /// Stops waiting for the index's lock if it isn't held yet, failing the requests queued
    /// meanwhile, so a server told to stop before the old one released the lock can exit.
    pub fn abandon_lock_wait(&self) {
        self.partitions.abandon_lock_wait();
    }

    #[must_use]
    pub fn get_key_page_index(&self, key: &str) -> usize {
        murmurhash32::murmurhash3_with_seed::<0>(key.as_bytes()) as usize % self.metadata.num_pages
    }

    /// Merges each page's new documents into it and writes it back. The pages' owner threads
    /// do the work, so the pages are done in parallel. A page that can't be written is
    /// skipped, and its owner says why.
    async fn store_pages(
        &self,
        page_documents: HashMap<usize, Vec<Document>>,
        mark_term_synced: Option<&String>,
    ) -> anyhow::Result<Vec<StoredPage>> {
        if matches!(self.mode, IndexMode::READ) {
            bail!("Index is in read only mode");
        }
        let num_pages = self.metadata.num_pages;
        if let Some(page) = page_documents.keys().find(|page| **page >= num_pages) {
            bail!("Page {page} is not in an index of {num_pages} pages");
        }
        let mut receivers = Vec::with_capacity(page_documents.len());
        for (page, docs) in page_documents {
            let (reply, receiver) = oneshot::channel();
            self.partitions.send(PageRequest::Merge {
                page,
                docs,
                mark_term_synced: mark_term_synced.cloned(),
                reply,
            })?;
            receivers.push(receiver);
        }
        let replies = join_all(receivers).await;
        // Owners that stopped waiting for the lock drop their requests unanswered, as they do
        // a page they skip, but this time nothing at all was written.
        if !self.partitions.holds_lock() {
            bail!("The index's lock was never taken, so nothing was written");
        }
        Ok(replies.into_iter().filter_map(Result::ok).collect())
    }

    pub async fn index_documents<I, T: BlacklistProvider>(
        &self,
        documents: I,
        blacklist: &T,
    ) -> anyhow::Result<IndexedDocuments>
    where
        I: IntoIterator<Item = Document>,
    {
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

        for page in self.store_pages(page_documents, None).await? {
            for stored in page.stored {
                if !matches!(stored.state(), Some(DocumentState::SyncedWithMainIndex))
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
        mark_term_synced: Option<&String>,
    ) -> anyhow::Result<HashMap<String, usize>> {
        let mut new_page_doc_counts: HashMap<String, usize> = HashMap::new();

        for page in self.store_pages(page_documents, mark_term_synced).await? {
            for stored in page.stored {
                if !matches!(stored.state(), Some(DocumentState::SyncedWithMainIndex))
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

    match TinyIndex::from_file(index_path, IndexMode::READ, 1) {
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

/// A document as a page holds it: the array Python's `Document.as_tuple` makes, in the same
/// field order with the trailing Nones cut off, so Python reads the pages Rust writes. Field
/// names would also cost page bytes, so a page would hold fewer documents.
pub struct PageItem<'a>(pub &'a Document);

impl Serialize for PageItem<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let document = self.0;
        let present = [
            document.score.is_some(),
            document.term.is_some(),
            document.state.is_some(),
            document.user_ids.is_some(),
            document.last_crawled.is_some(),
            document.source.is_some(),
        ];
        let num_optional = if document.extra.is_empty() {
            present
                .iter()
                .rposition(|is_some| *is_some)
                .map_or(0, |last| last + 1)
        } else {
            present.len()
        };
        let num_items = 3 + num_optional + document.extra.len();
        let mut items = serializer.serialize_seq(Some(num_items))?;
        items.serialize_element(&document.title)?;
        items.serialize_element(&document.url)?;
        items.serialize_element(&document.extract)?;
        if num_optional > 0 {
            items.serialize_element(&document.score)?;
        }
        if num_optional > 1 {
            items.serialize_element(&document.term)?;
        }
        if num_optional > 2 {
            items.serialize_element(&document.state)?;
        }
        if num_optional > 3 {
            items.serialize_element(&document.user_ids)?;
        }
        if num_optional > 4 {
            items.serialize_element(&document.last_crawled)?;
        }
        if num_optional > 5 {
            items.serialize_element(&document.source)?;
        }
        for extra in &document.extra {
            items.serialize_element(extra)?;
        }
        items.end()
    }
}

/// A document as a page holds it, read from either form a page may hold: the array Python
/// writes, or the object older crawlers wrote. An array may have more elements than this
/// build knows, written by a newer one. They are kept, to be written back unchanged, rather
/// than failing the page, which a merge would then reset.
pub struct PageDocument(pub Document);

/// The number of elements of a page item's array that this build reads.
const DOCUMENT_FIELDS: usize = 9;

impl<'de> Deserialize<'de> for PageDocument {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let mut value = Value::deserialize(deserializer)?;
        let extra = match &mut value {
            Value::Array(items) if items.len() > DOCUMENT_FIELDS => {
                items.split_off(DOCUMENT_FIELDS)
            }
            _ => Vec::new(),
        };
        let mut document = Document::deserialize(value).map_err(serde::de::Error::custom)?;
        document.extra = extra;
        Ok(PageDocument(document))
    }
}

/// A page's documents, and its version: a hash of its bytes as stored.
pub struct VersionedPage {
    pub documents: Vec<Document>,
    pub version: u64,
}

/// What a conditional page write did.
#[derive(Debug, PartialEq, Eq)]
pub enum ConditionalWrite {
    /// The page was still at the version given, and this many documents fit on it.
    Stored(usize),
    /// The page had changed, so nothing was written.
    Conflict,
}

#[derive(Default)]
pub struct StoredPage {
    pub(crate) stored: Vec<Document>,
    pub(crate) existing: Vec<Document>,
    pub(crate) stored_copies: Vec<Document>,
}

pub(crate) fn find_stored_copies<'b, 'a, I, J>(incoming: I, existing: J) -> Vec<Document>
where
    I: IntoIterator<Item = &'b Document>,
    J: IntoIterator<Item = &'a Document>,
{
    let mut submitters_by_url: HashMap<&str, HashSet<i32>> = HashMap::new();
    for document in incoming {
        let submitters = submitters_by_url.entry(&document.url).or_default();
        submitters.extend(document.user_ids.iter().flatten());
    }
    let mut stored_copies = Vec::new();
    for document in existing {
        // A curated copy or a copy of Staan's has no author of its own, yet takes on the
        // user_ids of the copies it beats, so its last id names one of them.
        let attributed =
            !document.state().is_some_and(DocumentState::is_curated) && document.source.is_none();
        // Another crawler's copy: the stored text may be this crawler's own, from an earlier
        // submission, and comparing that with itself says nothing.
        if attributed
            && let Some(&author_id) = document.user_ids.iter().flatten().last()
            && let Some(submitters) = submitters_by_url.get(document.url.as_str())
            && !submitters.is_empty()
            && !submitters.contains(&author_id)
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
pub(crate) fn combine_documents<'a, I, J, R: Ranker>(
    existing_documents: J,
    documents: I,
    mark_term_synced: Option<&String>,
    ranker: &R,
) -> Vec<Document>
where
    I: IntoIterator<Item = &'a Document>,
    J: IntoIterator<Item = &'a Document>,
{
    let sorted = sort_documents(existing_documents, documents, ranker);
    let mut values: Vec<(usize, Document)> = sorted
        .into_iter()
        .cloned()
        .enumerate()
        .map(|(order, doc)| (doc.url.clone(), (order, doc)))
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
        .map(|v| {
            let mut doc = v.1;
            if mark_term_synced.is_some()
                && doc.state.is_none()
                && doc.term.as_ref() == mark_term_synced
            {
                doc.state = Some(Coded::Known(DocumentState::SyncedWithMainIndex));
            }
            doc
        })
        .collect()
}

/// The first document, with the history of all of them: the latest crawl, and every
/// user id, latest crawl last. A document with no other copies is returned as it is.
fn merge_user_ids_last_crawled(mut docs: Vec<(usize, Document)>) -> Option<(usize, Document)> {
    if docs.len() <= 1 {
        return docs.pop();
    }
    let (idx, mut best_doc) = docs
        .first()
        .map(|(idx, best_doc)| (*idx, best_doc.to_owned()))?;
    docs.sort_by_key(|(_, doc)| doc.last_crawled.unwrap_or(i64::MIN));
    let mut user_ids: Vec<i32> = Vec::new();
    for (_, doc) in docs {
        best_doc.last_crawled = max(best_doc.last_crawled, doc.last_crawled);
        for &id in doc.user_ids.iter().flatten() {
            user_ids.retain(|v| *v != id);
            user_ids.push(id);
        }
    }
    best_doc.user_ids = Some(user_ids);
    Some((idx, best_doc))
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
        if doc.state().is_some_and(DocumentState::is_curated) {
            curated.push(doc);
        } else if matches!(doc.source(), Some(DocumentSource::Widget)) {
            widgets_urls.insert(doc.url.clone());
            widgets.push(doc);
        } else {
            existing_other.push(doc);
        }
    }

    let mut other_documents = Vec::new();

    for doc in documents {
        if matches!(doc.source(), Some(DocumentSource::Widget)) {
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
