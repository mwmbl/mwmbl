use std::collections::HashMap;
use std::os::unix::fs::FileExt;
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};

use mwmbl_core::index::pack_page;
use mwmbl_core::indexer::{
    Document, DocumentSource, DocumentState, IndexMode, PageItem, TinyIndex, create_index,
};
use serde_json::json;

#[test]
fn state_and_source_are_their_integer_values_as_the_server_has_them() {
    // The shape of a document in the server's `new_items`.
    let json = r#"{"title": "Rust", "url": "https://rust-lang.org/", "extract": "",
        "score": null, "term": "rust", "state": 7, "user_ids": null,
        "last_crawled": null, "source": 4}"#;
    let doc: Document = serde_json::from_str(json).unwrap();
    assert_eq!(doc.state(), Some(&DocumentState::OrganicApproved));

    let value = serde_json::to_value(&doc).unwrap();
    assert_eq!((&value["state"], &value["source"]), (&7.into(), &4.into()));
    let unknown = serde_json::from_str::<DocumentSource>("5").unwrap_err();
    assert!(unknown.to_string().contains("5 is not a DocumentSource"));
}

#[test]
fn pages_are_written_as_python_writes_them() {
    let text = |title: &str| {
        (
            title.to_owned(),
            "https://rust-lang.org/".to_owned(),
            "ext".to_owned(),
        )
    };
    let (title, url, extract) = text("Rust");
    let organic = Document::new(
        title,
        url,
        extract,
        None,
        Some("rust".to_owned()),
        None,
        None,
        None,
        None,
    );
    let (title, url, extract) = text("Curated");
    let curated = Document::new(
        title,
        url,
        extract,
        Some(0.5),
        Some("rust".to_owned()),
        Some(DocumentState::OrganicApproved),
        Some(vec![3]),
        Some(10),
        Some(DocumentSource::Widget),
    );
    let (title, url, extract) = text("Bare");
    let bare = Document::new(title, url, extract, None, None, None, None, None, None);
    // What Document.as_tuple() gives for each, as Python packs it.
    let from_python = [
        json!(["Rust", "https://rust-lang.org/", "ext", null, "rust"]),
        json!([
            "Curated",
            "https://rust-lang.org/",
            "ext",
            0.5,
            "rust",
            7,
            [3],
            10,
            4
        ]),
        json!(["Bare", "https://rust-lang.org/", "ext"]),
    ];

    let items = [PageItem(&organic), PageItem(&curated), PageItem(&bare)];
    assert_eq!(
        pack_page(4096, &items).unwrap(),
        pack_page(4096, &from_python).unwrap()
    );
}

#[test]
fn a_page_tuple_from_python_may_leave_off_its_trailing_nones() {
    let short: Document =
        serde_json::from_str(r#"["Rust", "https://rust-lang.org/", "ext"]"#).unwrap();
    assert_eq!((short.title(), short.state()), ("Rust", None));

    let json = r#"["Rust", "https://rust-lang.org/", "ext", 1.0, "rust", 7, [3], 1795000000]"#;
    let doc: Document = serde_json::from_str(json).unwrap();
    assert_eq!(doc.state(), Some(&DocumentState::OrganicApproved));
    assert_eq!(
        (doc.last_crawled(), doc.source()),
        (Some(1_795_000_000), None)
    );
}

#[test]
fn a_state_or_source_added_since_reads_as_none() {
    let json = r#"["Rust", "https://rust-lang.org/", "ext", null, "rust", 99, null, null, 99]"#;
    let doc: Document = serde_json::from_str(json).unwrap();
    assert_eq!((doc.state(), doc.source()), (None, None));
}

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

/// An index file removed when the test ends, so the tests need no tempfile crate.
struct ScratchIndex {
    path: PathBuf,
}

impl ScratchIndex {
    fn new(num_pages: usize) -> ScratchIndex {
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        let name = format!(
            "mwmbl_core_indexer_{}_{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        );
        let path = std::env::temp_dir().join(name);
        // create_index keeps an index already at the path, and the test needs an empty one.
        assert!(!path.exists(), "{} is left over", path.display());
        create_index(&path, num_pages, false).unwrap();
        ScratchIndex { path }
    }
}

impl Drop for ScratchIndex {
    fn drop(&mut self) {
        std::fs::remove_file(&self.path).unwrap();
    }
}

#[tokio::test]
async fn copies_of_a_url_merge_their_history_and_a_title_is_kept_once() {
    let scratch = ScratchIndex::new(8);
    let index = TinyIndex::from_file(&scratch.path, IndexMode::WRITE, 2).unwrap();
    let page = index.get_key_page_index("rust");
    let existing = vec![
        document("Rust", "https://rust-lang.org/", 10, 1),
        document("Other", "https://other.example/rust", 5, 4),
    ];
    let incoming = vec![
        document("Rust", "https://rust-lang.org/", 20, 2),
        document("Rust", "https://copy.example/rust", 30, 3),
    ];
    index
        .index_pages(HashMap::from([(page, existing)]), None)
        .await
        .unwrap();

    index
        .index_pages(HashMap::from([(page, incoming)]), Some(&"rust".to_owned()))
        .await
        .unwrap();
    let combined = index.retrieve("rust").await.unwrap();

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

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn concurrent_writes_to_one_page_lose_no_documents() {
    let scratch = ScratchIndex::new(4);
    let index = TinyIndex::from_file(&scratch.path, IndexMode::WRITE, 3).unwrap();
    let page = index.get_key_page_index("rust");
    let tasks: Vec<_> = (0..20)
        .map(|n| {
            let index = index.clone();
            let doc = document(&format!("Rust {n}"), &format!("https://{n}.example/"), n, 1);
            tokio::spawn(async move {
                index
                    .index_pages(HashMap::from([(page, vec![doc])]), None)
                    .await
            })
        })
        .collect();
    for task in tasks {
        task.await.unwrap().unwrap();
    }

    let stored = index.retrieve("rust").await.unwrap();
    assert_eq!(stored.len(), 20);
}

#[tokio::test]
async fn a_corrupt_page_is_reset_by_the_next_write() {
    let scratch = ScratchIndex::new(4);
    let index = TinyIndex::from_file(&scratch.path, IndexMode::WRITE, 2).unwrap();
    let page = index.get_key_page_index("rust");
    let offset = u64::try_from((page + 1) * 4096).unwrap();
    let file = std::fs::OpenOptions::new()
        .write(true)
        .open(&scratch.path)
        .unwrap();
    file.write_all_at(&[0xff; 4096], offset).unwrap();
    assert!(index.retrieve("rust").await.is_err());

    let doc = document("Rust", "https://rust-lang.org/", 1, 1);
    index
        .index_pages(HashMap::from([(page, vec![doc])]), None)
        .await
        .unwrap();

    let stored = index.retrieve("rust").await.unwrap();
    assert_eq!(stored.len(), 1);
}

#[test]
fn only_one_handle_may_write_an_index_at_a_time() {
    let scratch = ScratchIndex::new(4);
    let writer = TinyIndex::from_file(&scratch.path, IndexMode::WRITE, 1).unwrap();
    let Err(error) = TinyIndex::from_file(&scratch.path, IndexMode::WRITE, 1) else {
        panic!("a second writer opened the index");
    };
    assert!(
        error.to_string().contains("already open for writing"),
        "{error}"
    );
    TinyIndex::from_file(&scratch.path, IndexMode::READ, 1).unwrap();

    drop(writer);
    TinyIndex::from_file(&scratch.path, IndexMode::WRITE, 1).unwrap();
}

#[tokio::test]
async fn a_page_outside_the_index_is_an_error() {
    let scratch = ScratchIndex::new(4);
    let index = TinyIndex::from_file(&scratch.path, IndexMode::WRITE, 1).unwrap();
    let page = index.get_key_page_index("rust");
    let doc = document("Rust", "https://rust-lang.org/", 1, 1);
    let page_documents = HashMap::from([(page, vec![doc.clone()]), (4, vec![doc])]);
    assert!(index.index_pages(page_documents, None).await.is_err());
    assert!(index.retrieve("rust").await.unwrap().is_empty());
}
