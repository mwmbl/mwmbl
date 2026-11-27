//! The owner threads of an index's pages. Page `p` is only ever read or written by thread
//! `p % K`, so no page needs a lock, and a read always sees the writes queued before it.

use std::{
    collections::HashMap,
    fs::File,
    sync::{Arc, mpsc},
    thread::{self, JoinHandle},
};

use anyhow::{Context, bail};
use tokio::sync::oneshot;

use crate::{
    constants::METADATA_SIZE,
    index::{PageIoError, read_index_page, write_index_page},
    indexer::{Document, StoredPage, combine_documents, find_stored_copies},
    rank::HeuristicRanker,
};

/// The most requests an owner thread takes off its queue before applying them, so a read
/// behind a long stream of writes still gets an answer.
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

/// Merges for one page that are applied with a single read and write. Only merges that
/// mark the same term as synced are grouped, since the mark applies to the whole page.
struct MergeGroup {
    mark_term_synced: Option<String>,
    requests: Vec<(Vec<Document>, oneshot::Sender<StoredPage>)>,
}

struct Owner {
    file: Arc<File>,
    page_size: usize,
}

impl Owner {
    fn run(&self, receiver: &mpsc::Receiver<PageRequest>) {
        while let Ok(first) = receiver.recv() {
            let round =
                std::iter::once(first).chain(receiver.try_iter().take(MAX_REQUESTS_PER_ROUND));
            let mut pending: HashMap<usize, MergeGroup> = HashMap::new();
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
                        let marks_differ = pending
                            .get(&page)
                            .is_some_and(|group| group.mark_term_synced != mark_term_synced);
                        if marks_differ && let Some(group) = pending.remove(&page) {
                            self.merge(page, group);
                        }
                        pending
                            .entry(page)
                            .or_insert_with(|| MergeGroup {
                                mark_term_synced,
                                requests: Vec::new(),
                            })
                            .requests
                            .push((docs, reply));
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

    fn merge(&self, page: usize, group: MergeGroup) {
        let offset = self.offset(page);
        let existing: Vec<Document> =
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
        let incoming = group.requests.iter().flat_map(|(docs, _)| docs);
        let mut combined = combine_documents(
            &existing,
            incoming,
            group.mark_term_synced.as_ref(),
            &HeuristicRanker::new(f32::NEG_INFINITY),
        );
        match write_index_page(&self.file, offset, self.page_size, &combined) {
            Ok(num_stored) => combined.truncate(num_stored),
            Err(error) => {
                eprintln!("Skipping index page {page}: {error}");
                return;
            }
        }
        for (docs, reply) in group.requests {
            let stored_copies = find_stored_copies(&docs, &existing);
            let stored_page = StoredPage {
                stored: combined.clone(),
                existing: existing.clone(),
                stored_copies,
            };
            respond(reply, stored_page);
        }
    }
}

fn respond<T>(reply: oneshot::Sender<T>, value: T) {
    if reply.send(value).is_err() {
        eprintln!("An index request was abandoned before its reply");
    }
}
