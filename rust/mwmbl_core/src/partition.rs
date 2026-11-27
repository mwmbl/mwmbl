//! The owner threads of an index's pages. Page `p` is only ever read or written by thread
//! `p % K`, so no page needs a lock, and a read always sees the writes queued before it.

use std::{
    collections::HashMap,
    fs::File,
    os::unix::fs::FileExt,
    sync::{Arc, mpsc},
    thread::{self, JoinHandle},
};

use anyhow::{Context, bail};
use tokio::sync::oneshot;

use crate::{
    constants::METADATA_SIZE,
    index::{PageIoError, pack_page, read_index_page},
    indexer::{Document, StoredPage, combine_documents, find_stored_copies},
    rank::HeuristicRanker,
};

/// The most requests an owner thread takes off its queue in one round. Merges are written
/// at the end of their round, so this bounds how long they, and the requests queued after
/// the round, wait.
const MAX_REQUESTS_PER_ROUND: usize = 1024;

pub(crate) enum PageRequest {
    Retrieve {
        page: usize,
        term: String,
        reply: oneshot::Sender<Result<Vec<Document>, PageIoError>>,
    },
    /// Dropping the reply unanswered means the page was skipped; the owner says why.
    Merge {
        page: usize,
        docs: Vec<Document>,
        mark_term_synced: Option<String>,
        reply: oneshot::Sender<StoredPage>,
    },
}

impl PageRequest {
    fn page(&self) -> usize {
        match self {
            PageRequest::Retrieve { page, .. } | PageRequest::Merge { page, .. } => *page,
        }
    }
}

pub(crate) struct Partitions {
    senders: Vec<mpsc::Sender<PageRequest>>,
    threads: Vec<JoinHandle<()>>,
    num_pages: usize,
}

impl Partitions {
    pub(crate) fn start(
        file: &Arc<File>,
        page_size: usize,
        num_pages: usize,
        num_threads: usize,
    ) -> anyhow::Result<Partitions> {
        if num_threads == 0 {
            bail!("An index needs at least one owner thread");
        }
        let mut senders = Vec::with_capacity(num_threads);
        let mut threads = Vec::with_capacity(num_threads);
        for number in 0..num_threads {
            let (sender, receiver) = mpsc::channel();
            let owner = Owner {
                file: Arc::clone(file),
                page_size,
            };
            let thread = thread::Builder::new()
                .name(format!("index-owner-{number}"))
                .spawn(move || owner.run(&receiver))
                .context("Could not start an index owner thread")?;
            senders.push(sender);
            threads.push(thread);
        }
        Ok(Partitions {
            senders,
            threads,
            num_pages,
        })
    }

    pub(crate) fn send(&self, request: PageRequest) -> anyhow::Result<()> {
        let page = request.page();
        if page >= self.num_pages {
            bail!("Page {page} is not in an index of {} pages", self.num_pages);
        }
        let Some(sender) = self.senders.get(page % self.senders.len()) else {
            bail!("No owner thread for page {page}");
        };
        if sender.send(request).is_err() {
            bail!("The owner thread of page {page} has stopped");
        }
        Ok(())
    }
}

impl Drop for Partitions {
    fn drop(&mut self) {
        // Closing the channels is what tells the threads to finish their queues and stop.
        self.senders.clear();
        for thread in self.threads.drain(..) {
            if thread.join().is_err() {
                eprintln!("An index owner thread panicked");
            }
        }
    }
}

/// One merge waiting for its page's next read and write.
struct Merge {
    docs: Vec<Document>,
    mark_term_synced: Option<String>,
    reply: oneshot::Sender<StoredPage>,
}

struct Owner {
    file: Arc<File>,
    page_size: usize,
}

/// Aborts the process if an owner thread panics. Without it the other partitions carry on
/// and every request for the dead one fails, so a crawler keeps running but submits
/// nothing; aborting lets Docker's restart policy start it afresh.
struct AbortOnPanic;

impl Drop for AbortOnPanic {
    fn drop(&mut self) {
        if thread::panicking() {
            eprintln!("An index owner thread panicked; aborting");
            std::process::abort();
        }
    }
}

impl Owner {
    fn run(&self, receiver: &mpsc::Receiver<PageRequest>) {
        let _abort_on_panic = AbortOnPanic;
        while let Ok(first) = receiver.recv() {
            let round =
                std::iter::once(first).chain(receiver.try_iter().take(MAX_REQUESTS_PER_ROUND));
            let mut pending: HashMap<usize, Vec<Merge>> = HashMap::new();
            for request in round {
                match request {
                    PageRequest::Retrieve { page, term, reply } => {
                        if let Some(group) = pending.remove(&page) {
                            self.merge(page, group);
                        }
                        let documents = read_index_page(
                            &self.file,
                            self.offset(page),
                            self.page_size,
                            Some(&term),
                        );
                        respond(reply, documents);
                    }
                    PageRequest::Merge {
                        page,
                        docs,
                        mark_term_synced,
                        reply,
                    } => {
                        pending.entry(page).or_default().push(Merge {
                            docs,
                            mark_term_synced,
                            reply,
                        });
                    }
                }
            }
            for (page, group) in pending {
                self.merge(page, group);
            }
        }
    }

    fn offset(&self, page: usize) -> u64 {
        (page * self.page_size + METADATA_SIZE!()) as u64
    }

    /// Applies the merges to the page in order, each to the page as the one before left it,
    /// so each reply is what applying the merges one at a time would give. Only the result
    /// is written, once. The replies are sent after that write, and a merge whose documents
    /// can't be packed is skipped as it would have been alone.
    fn merge(&self, page: usize, merges: Vec<Merge>) {
        let offset = self.offset(page);
        let mut state: Vec<Document> =
            match read_index_page(&self.file, offset, self.page_size, None) {
                Ok(existing) => existing,
                // This thread is the page's only writer, so this is real damage rather than a
                // write caught half-done. Nothing else would ever repair it.
                Err(PageIoError::Unreadable(reason)) => {
                    eprintln!("Index page {page} is corrupt ({reason}); resetting it");
                    Vec::new()
                }
                Err(error) => {
                    eprintln!("Skipping index page {page}: {error}");
                    return;
                }
            };
        let ranker = HeuristicRanker::new(f32::NEG_INFINITY);
        let mut packed = None;
        let mut replies = Vec::with_capacity(merges.len());
        for merge in merges {
            let stored_copies = find_stored_copies(&merge.docs, &state);
            let mut combined = combine_documents(
                &state,
                &merge.docs,
                merge.mark_term_synced.as_ref(),
                &ranker,
            );
            let (bytes, num_stored) = match pack_page(self.page_size, &combined) {
                Ok(page_bytes) => page_bytes,
                Err(error) => {
                    eprintln!("Skipping a merge into index page {page}: {error}");
                    continue;
                }
            };
            combined.truncate(num_stored);
            packed = Some(bytes);
            let existing = std::mem::replace(&mut state, combined);
            let stored_page = StoredPage {
                stored: state.clone(),
                existing,
                stored_copies,
            };
            replies.push((merge.reply, stored_page));
        }
        let Some(bytes) = packed else {
            return;
        };
        if let Err(error) = self.file.write_all_at(&bytes, offset) {
            eprintln!("Skipping index page {page}: {error}");
            return;
        }
        for (reply, stored_page) in replies {
            respond(reply, stored_page);
        }
    }
}

fn respond<T>(reply: oneshot::Sender<T>, value: T) {
    if reply.send(value).is_err() {
        eprintln!("An index request was abandoned before its reply");
    }
}

#[cfg(test)]
mod tests {
    use std::{fs::OpenOptions, path::PathBuf};

    use super::*;
    use crate::indexer::create_index;

    fn document(url: &str, title: &str, user_id: i32) -> Document {
        Document::new(
            title.to_owned(),
            url.to_owned(),
            "rust is a language".to_owned(),
            None,
            Some("rust".to_owned()),
            None,
            Some(vec![user_id]),
            Some(1),
            None,
        )
    }

    fn owner(name: &str) -> (Owner, PathBuf) {
        let path =
            std::env::temp_dir().join(format!("mwmbl_partition_{}_{name}", std::process::id()));
        create_index(&path, 4, false).unwrap();
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .open(&path)
            .unwrap();
        let owner = Owner {
            file: Arc::new(file),
            page_size: 4096,
        };
        (owner, path)
    }

    fn merges(batches: Vec<Vec<Document>>) -> (Vec<Merge>, Vec<oneshot::Receiver<StoredPage>>) {
        batches
            .into_iter()
            .map(|docs| {
                let (reply, receiver) = oneshot::channel();
                let merge = Merge {
                    docs,
                    mark_term_synced: None,
                    reply,
                };
                (merge, receiver)
            })
            .unzip()
    }

    fn summary(receiver: oneshot::Receiver<StoredPage>) -> serde_json::Value {
        let page = receiver.blocking_recv().unwrap();
        serde_json::json!({
            "stored": page.stored,
            "existing": page.existing,
            "stored_copies": page.stored_copies,
        })
    }

    #[test]
    fn grouped_merges_reply_as_sequential_merges_would() {
        let batches = || {
            vec![
                vec![document("https://rust-lang.org/", "Rust", 1)],
                vec![
                    document("https://rust-lang.org/", "Rust", 1),
                    document("https://other.example/", "Other", 2),
                ],
            ]
        };
        let (grouped_owner, grouped_path) = owner("grouped");
        let (grouped, grouped_receivers) = merges(batches());
        grouped_owner.merge(0, grouped);
        let (sequential_owner, sequential_path) = owner("sequential");
        let mut sequential_replies = Vec::new();
        for batch in batches() {
            let (sequential, receivers) = merges(vec![batch]);
            sequential_owner.merge(0, sequential);
            sequential_replies.extend(receivers.into_iter().map(summary));
        }

        let grouped_replies: Vec<_> = grouped_receivers.into_iter().map(summary).collect();
        assert_eq!(grouped_replies, sequential_replies);
        let second = &grouped_replies[1];
        assert_eq!(second["existing"].as_array().unwrap().len(), 1, "{second}");
        assert_eq!(
            second["stored_copies"].as_array().unwrap().len(),
            1,
            "{second}"
        );
        let read = |path: &PathBuf| std::fs::read(path).unwrap();
        assert_eq!(read(&grouped_path), read(&sequential_path));
        std::fs::remove_file(grouped_path).unwrap();
        std::fs::remove_file(sequential_path).unwrap();
    }

    #[test]
    fn a_retrieve_in_the_same_round_as_a_merge_sees_it() {
        let (owner, path) = owner("round");
        let (sender, receiver) = mpsc::channel();
        let (mut queued, _merge_receivers) =
            merges(vec![vec![document("https://rust-lang.org/", "Rust", 1)]]);
        let merge = queued.remove(0);
        let (reply, retrieved) = oneshot::channel();
        sender
            .send(PageRequest::Merge {
                page: 0,
                docs: merge.docs,
                mark_term_synced: None,
                reply: merge.reply,
            })
            .unwrap();
        sender
            .send(PageRequest::Retrieve {
                page: 0,
                term: "rust".to_owned(),
                reply,
            })
            .unwrap();
        drop(sender);
        owner.run(&receiver);

        assert_eq!(retrieved.blocking_recv().unwrap().unwrap().len(), 1);
        std::fs::remove_file(path).unwrap();
    }
}
