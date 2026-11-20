use std::{
    collections::{HashMap, HashSet},
    fs::{self, File},
    io::{Read, Write},
    os::fd::AsRawFd,
    path::Path,
    str::FromStr,
};

use anyhow::{Context, bail};
use file_guard::Lock;
use itertools::Itertools;
use reqwest::Url;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::{
    blacklist_providers::BlacklistProvider,
    constants::{
        DOCUMENT_STATE_CURATED_THRESHOLD, INDEX_VERSION, ITEM_FACTORY, METADATA_CONSTANT,
        METADATA_SIZE, PAGE_SIZE,
    },
    index::{PageIoError, pack_page, read_index_page, tokenize_document},
    rank::{HeuristicRanker, Ranker},
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
    WIKIPEDIA = 1,
    STAAN = 2,
    StaanIndexed = 3,
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

#[derive(Debug)]
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

#[derive(Serialize, Deserialize, Debug)]
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

#[derive(Debug)]
pub struct TinyIndex {
    index_file: File,
    mode: IndexMode,
    metadata: TinyIndexMetadata,
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

        Ok(Self {
            index_file: file,
            mode,
            metadata,
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

    fn lock_page(&self, page: usize) -> anyhow::Result<file_guard::FileGuard<&File>> {
        let start: usize = page * self.metadata.page_size + METADATA_SIZE!();
        if matches!(self.mode, IndexMode::READ) {
            let lock = file_guard::lock(
                &self.index_file,
                Lock::Shared,
                start,
                self.metadata.page_size,
            )?;
            return Ok(lock);
        }

        let lock = file_guard::lock(
            &self.index_file,
            Lock::Exclusive,
            start,
            self.metadata.page_size,
        )?;

        Ok(lock)
    }

    fn get_page<T: for<'a> Deserialize<'a>>(
        lock: &file_guard::FileGuard<&File>,
        term: Option<&String>,
    ) -> anyhow::Result<Vec<T>> {
        Ok(read_index_page(
            lock.as_raw_fd(),
            lock.offset() as u64,
            lock.len(),
            term,
        )?)
    }

    #[must_use]
    pub fn get_key_page_index(&self, key: &str) -> usize {
        murmurhash32::murmurhash3_with_seed::<0>(key.as_bytes()) as usize % self.metadata.num_pages
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

pub async fn index_batch<I, T: BlacklistProvider>(
    documents: I,
    index_path: &Path,
    blacklist: &T,
) -> anyhow::Result<()>
where
    I: IntoIterator<Item = Document>,
{
    let index = TinyIndex::from_file(index_path, IndexMode::READ)?;

    let mut page_documents = Vec::new();

    for mut doc in documents {
        if let Ok(url) = Url::from_str(&doc.url)
            && let Some(domain) = url.domain()
            && !blacklist.is_domain_blacklisted(domain).await
        {
            doc.title = doc.title.escape_unicode().collect();
            doc.extract = doc.extract.escape_unicode().collect();

            let tokens = tokenize_document(&doc.url, &doc.title, &doc.extract);
            for token in tokens {
                let page = index.get_key_page_index(&token);
                let mut doc = doc.clone();
                doc.term = Some(token);
                page_documents.push((page, doc));
            }
        }
    }
    let page_documents = page_documents.into_iter().into_group_map();

    Ok(())
}

fn store_pages<I, J>(
    index_path: &Path,
    page_documents: I,
    mark_synced: bool,
) -> anyhow::Result<()>
where
    I: IntoIterator<Item = (usize, J)>,
    J: IntoIterator<Item = Document>,
{
    let index = TinyIndex::from_file(index_path, IndexMode::WRITE)?;

    for (page, docs) in page_documents {
        if let Ok(lock) = index.lock_page(page) {
            let existing_documents: Vec<Document> = TinyIndex::get_page(&lock, None)?;
            let stored_copy = find_stored_copies(docs, existing_documents);
            // let combined_documents = combine_documents(existing_documents, documents, mark_synced, HeuristicRanker::new(f64::NEG_INFINITY));
            todo!()
        } else {
            eprintln!("Skipping index page {page}");
        }
    }
    Ok(())
}

fn find_stored_copies<I, J>(incoming: I, existing: J) -> Vec<Document>
where
    I: IntoIterator<Item = Document>,
    J: IntoIterator<Item = Document>,
{
    let submitters_by_url = incoming
        .into_iter()
        .filter_map(|v| {
            if let Some(user_ids) = v.user_ids {
                Some((v.url, user_ids))
            } else {
                None
            }
        })
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
    fn is_curated(&self) -> bool {
        matches!(
            self,
            DocumentState::OrganicApproved
                | DocumentState::FromUserApproved
                | DocumentState::FromGoogleApproved
                | DocumentState::FromWikiApproved
        )
    }
}

fn combine_documents<I, J, R: Ranker>(
    existing_documents: &J,
    documents: &I,
    mark_synced: bool,
    ranker: &R,
) -> Vec<Document>
where
    I: IntoIterator<Item = Document>,
    J: IntoIterator<Item = Document>,
{
    todo!()
}
