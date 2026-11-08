use std::{fs::{self, File}, io::{Read, Write}, path::{Path, PathBuf}};

use anyhow::{Context, bail};
use serde::{Deserialize, Serialize};

use crate::{constants::{INDEX_VERSION, ITEM_FACTORY, METADATA_CONSTANT, METADATA_SIZE, PAGE_SIZE}, index::{PageIoError, pack_page}};


/// The state of the document in the index. A value of None indicates an organic search result
pub enum DocumentState {
    SyncedWithMainIndex = -2,
    DELETED = -1,
    FromUser = 2,
    FromGoogle = 3,
    FromWiki = 4,
    OrganicApproved = 7,
    FromUserApproved = 8,
    FromGoogleApproved = 9,
    FromWikiApproved = 10
}

pub enum DocumentSource {
    WIKIPEDIA = 1,
    STAAN = 2,
    StaanIndexed = 3,
}

pub struct Document {
    title: String,
    url: String,
    extract: String,
    score: Option<f32>,
    term: Option<String>,
    state: Option<DocumentState>,
    user_ids: Option<Vec<i32>>,
    last_crawled: Option<i32>,
    source: Option<DocumentSource>
}

#[derive(Debug)]
pub enum IndexMode {
    READ,
    WRITE
}


impl Document {
    #[allow(clippy::too_many_arguments)] fn new(_title: String, _url: String, _extract: String, _score: Option<f32>, _term: Option<String>, _state: Option<DocumentState>, _user_ids: Option<Vec<i32>>, _last_crawled: Option<i32>, _source: Option<DocumentSource>) -> Document {
        todo!()
    }
}

#[derive(Serialize, Deserialize, Debug)]
pub struct TinyIndexMetadata {
    version: i32,
    page_size: usize,
    num_pages: usize,
    item_factory: String
}


impl TinyIndexMetadata {
    fn new(page_size: usize, num_pages: usize) -> TinyIndexMetadata {
        TinyIndexMetadata { version: INDEX_VERSION, page_size, num_pages, item_factory: ITEM_FACTORY.to_owned() }
    }
    fn to_bytes(&self) -> Result<[u8; METADATA_SIZE], PageIoError> {
        let mut bytes: [u8; METADATA_SIZE]= [0; METADATA_SIZE];
        if let Some(slice) = bytes.get_mut(..METADATA_CONSTANT.len()){
            slice.copy_from_slice(METADATA_CONSTANT);
        } else {
            return Err(PageIoError::Unreadable(format!("METADATA_CONSTANT is too big for page size ({METADATA_SIZE})")));
        }
        let json = serde_json::to_string(self).map_err(|err| PageIoError::Io(err.into()))?;

        if let Some(slice) = bytes.get_mut(METADATA_CONSTANT.len()..METADATA_CONSTANT.len()+json.len()){
            slice.copy_from_slice(json.as_bytes());
        } else {
            return Err(PageIoError::Unreadable(format!("Metadata is too big for page size ({METADATA_SIZE})")));
        }
        Ok(bytes)
    }
    fn from_bytes(bytes: &[u8]) -> Result<TinyIndexMetadata, PageIoError> {
        if !bytes.starts_with(METADATA_CONSTANT) {
            return Err(PageIoError::Unreadable("This doesn't seem to be an index file".to_owned()));
        }
        if let Some(slice) = bytes.get(METADATA_CONSTANT.len()..){
            let json: Vec<u8> = slice.iter().copied().take_while(|e| *e != 0).collect();
            serde_json::from_slice(json.as_slice()).map_err(|err| PageIoError::Io(err.into()))
        } else {
            Err(PageIoError::Unreadable("Not enought bytes for TinyIndexMetadata".to_owned()))
        }
    }
}

#[derive(Debug)]
pub struct TinyIndex {
    index_path: PathBuf,
    mode: IndexMode,
    metadata: TinyIndexMetadata
}

impl TinyIndex {
    pub fn from_file(path: &Path, mode: IndexMode) -> anyhow::Result<TinyIndex> {
        let mut file = File::open(path)?;
        let mut metadata_bytes: [u8; 4096] = [0; METADATA_SIZE];
        file.read_exact(&mut metadata_bytes)?;
        
        let metadata = TinyIndexMetadata::from_bytes(&metadata_bytes)?;
        if metadata.item_factory != ITEM_FACTORY {
            bail!("Metadata item factory '{}' in the index does not match the passed item factory: '{}", metadata.item_factory, ITEM_FACTORY);
        }

        Ok(Self{
            index_path: path.to_path_buf(),
            mode,
            metadata,
        })
    }
    fn create(path: &Path, num_pages: usize, overwrite: bool) -> anyhow::Result<TinyIndex> {
        if path.is_file() && !overwrite {
            bail!("Index file '{}' already exists", path.display());
        }
        let metadata = TinyIndexMetadata::new(METADATA_SIZE, num_pages);
        let bytes= metadata.to_bytes()?;

        let page_bytes = pack_page(PAGE_SIZE, &[])?;
        
        let mut file = File::create(path)?;
        file.write_all(&bytes)?;
        for _ in 0..num_pages {
            file.write_all(&page_bytes.0)?;
        }
        file.flush()?;
        Self::from_file(path, IndexMode::READ)
    }
    fn validate(&self, num_pages: usize, page_size: usize) -> anyhow::Result<()> {
        if self.metadata.num_pages != num_pages || self.metadata.page_size != page_size{
            bail!("Existing index page sizes ({}) or number of pages ({}) do not match", self.metadata.num_pages, self.metadata.page_size);
        }
        Ok(())
    }
}


pub fn create_index(index_path: &Path, num_pages: usize, rebuild_on_mismatch: bool) -> anyhow::Result<()> {
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
            let validation = index.validate(num_pages, PAGE_SIZE);
            if !rebuild_on_mismatch {
                validation?;
                return Ok(());
            }
            println!("rebuilding index");
            TinyIndex::create(index_path, num_pages, true)?;
        },
        Err(err) => {
            if !rebuild_on_mismatch {
                return Err(err);
            }
            println!("Invalid index - rebuilding {}", index_path.display());
            TinyIndex::create(index_path, num_pages, true)?;
        },
    }
    
    Ok(())
}