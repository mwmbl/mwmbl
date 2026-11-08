use std::{fs, path::{Path, PathBuf}};

use anyhow::{Context};


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

impl Document {
    #[allow(clippy::too_many_arguments)] fn new(_title: String, _url: String, _extract: String, _score: Option<f32>, _term: Option<String>, _state: Option<DocumentState>, _user_ids: Option<Vec<i32>>, _last_crawled: Option<i32>, _source: Option<DocumentSource>) -> Document {
        todo!()
    }
}

pub struct TinyIndexMetadata {
    version: i32,
    page_size: i32,
    num_pages: i32,
    item_factory: String
}

pub struct TinyIndex {

}

impl TinyIndex {
    fn from_file(_path: &PathBuf) -> anyhow::Result<TinyIndex> {
        todo!()
    }
    fn create(path: &Path, _num_pages: i32, _overwrite: bool) -> anyhow::Result<TinyIndex> {
        if path.is_file() {
            todo!()
        }
        todo!()
    }
    fn validate(&self) -> anyhow::Result<()> {
        todo!()
    }
}


pub fn create_index(index_path: &Path, num_pages: i32, rebuild_on_mismatch: bool) -> anyhow::Result<()> {
    let index_path = index_path.canonicalize()?;
    fs::create_dir_all(index_path.parent().context("Invalid index path")?)?;

    if !index_path.exists() {
        println!("======================================");
        println!("Index not found - creating a new index");
        println!("======================================");
        TinyIndex::create(&index_path, num_pages, true)?;
        return Ok(());
    }

    match TinyIndex::from_file(&index_path) {
        Ok(_) => {
            println!("======================================");
            println!("Found existing index at {}", index_path.display());
            println!("======================================");
        },
        Err(err) => {
            if !rebuild_on_mismatch {
                return Err(err);
            }
            println!("Invalid index - rebuilding {}", index_path.display());
            TinyIndex::create(&index_path, num_pages, true)?;
        },
    }
    
    Ok(())
}