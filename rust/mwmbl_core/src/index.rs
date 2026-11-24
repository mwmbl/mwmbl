//! `TinyIndex` pages, and the tokens a document is indexed under.
//!
//! A page is a zstd frame holding a JSON array of documents, padded with zeros to the page
//! size. The page functions take the descriptor of a file the caller has open and leave it
//! open, so that `mwmbl_rank` can read and write pages with the GIL released while Python
//! keeps the file, the metadata and the page locks.

use std::cell::RefCell;
use std::collections::HashSet;
use std::error::Error;
use std::fmt;
use std::fs::File;
use std::io;
use std::iter::zip;
use std::mem::ManuallyDrop;
use std::os::unix::fs::FileExt;
use std::os::unix::io::FromRawFd;

use serde::Serialize;
use serde::de::DeserializeOwned;
use serde_json::Value;
use zstd::bulk::{Compressor, Decompressor};
use zstd::zstd_safe;

use crate::constants::NUM_FIRST_TOKENS;
use crate::tokenizer::{get_bigrams, tokenize};

/// Where the term sits in a document written as a tuple. Python's `Document.as_tuple` drops
/// trailing `None`s, so a document with no term may stop before it.
const TERM_INDEX: usize = 4;

/// python-zstandard's default level, which wrote the pages already in the index.
const COMPRESSION_LEVEL: i32 = 3;

/// How many times the page's size its contents may claim to be. It bounds what a corrupt frame
/// header can make `decompress_page` allocate.
const MAX_PAGE_EXPANSION: usize = 1024;

/// A failed page read or write, split by how callers handle it: `mwmbl_rank` raises an
/// `OSError` for `Io` and a `PageError` for `Unreadable`.
#[derive(Debug)]
pub enum PageIoError {
    /// The read or the write failed, or zstd could not make a context or compress.
    Io(io::Error),
    /// The page is short, corrupt or half-written, or the documents won't fit on one.
    Unreadable(String),
}

impl PageIoError {
    fn unreadable(error: impl fmt::Display) -> PageIoError {
        PageIoError::Unreadable(error.to_string())
    }
}

impl fmt::Display for PageIoError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            PageIoError::Io(error) => write!(f, "PageIoError::Io: {error}"),
            PageIoError::Unreadable(error) => write!(f, "PageIoError::Unreadable: {error}"),
        }
    }
}

impl Error for PageIoError {}

/// The file behind `fd`, which belongs to the caller: dropping a plain `File` would close it.
fn borrow_file(fd: i32) -> ManuallyDrop<File> {
    // SAFETY: the callers pass a descriptor they keep open for the whole call, and the
    // ManuallyDrop never closes it.
    ManuallyDrop::new(unsafe { File::from_raw_fd(fd) })
}

/// A positioned read stops at the end of the file, so a short one means the file isn't the
/// shape the metadata says: truncated, or half-written by create. That is an unreadable page,
/// because decoding the fragment would let a writer merge onto a page it never read.
fn read_error(error: io::Error) -> PageIoError {
    if error.kind() == io::ErrorKind::UnexpectedEof {
        return PageIoError::Unreadable(format!("could not read a whole page: {error}"));
    }
    PageIoError::Io(error)
}

fn read_page_bytes(fd: i32, offset: u64, page_size: usize) -> Result<Vec<u8>, PageIoError> {
    let file = borrow_file(fd);
    let mut page = vec![0u8; page_size];
    // read_exact_at rather than read_at, which can stop short and doesn't retry a read a
    // signal interrupted.
    file.read_exact_at(&mut page, offset).map_err(read_error)?;
    Ok(page)
}

thread_local! {
    // Made on first use and kept for every page the thread reads or writes: making one costs
    // about as much as compressing a page, and packing a full page compresses about a dozen
    // prefixes. Each call handles a whole frame, so nothing carries over from a failed page.
    static DECOMPRESSOR: RefCell<Option<Decompressor<'static>>> = const { RefCell::new(None) };
    static COMPRESSOR: RefCell<Option<Compressor<'static>>> = const { RefCell::new(None) };
}

fn decompress(frame: &[u8], contents_size: usize) -> Result<Vec<u8>, PageIoError> {
    DECOMPRESSOR.with_borrow_mut(|decompressor| {
        let decompressor = match decompressor {
            Some(decompressor) => decompressor,
            None => decompressor.insert(Decompressor::new().map_err(PageIoError::Io)?),
        };
        decompressor
            .decompress(frame, contents_size)
            .map_err(PageIoError::unreadable)
    })
}

fn compress(contents: &[u8]) -> Result<Vec<u8>, PageIoError> {
    COMPRESSOR.with_borrow_mut(|compressor| {
        let compressor = match compressor {
            Some(compressor) => compressor,
            None => compressor.insert(Compressor::new(COMPRESSION_LEVEL).map_err(PageIoError::Io)?),
        };
        compressor.compress(contents).map_err(PageIoError::Io)
    })
}

/// The contents of the zstd frame at the start of `page`, ignoring the padding after it.
fn decompress_page(page: &[u8]) -> Result<Vec<u8>, PageIoError> {
    let frame_size = zstd_safe::find_frame_compressed_size(page).map_err(zstd_error)?;
    let Some(frame) = page.get(..frame_size) else {
        return Err(PageIoError::unreadable("Invalid frame size"));
    };
    // Every frame the index writes records its content size, so the buffer can be exactly
    // that big.
    let recorded_size =
        zstd_safe::get_frame_content_size(frame).map_err(PageIoError::unreadable)?;
    let Some(recorded_size) = recorded_size else {
        return Err(PageIoError::unreadable("The frame has no content size"));
    };
    let contents_size = usize::try_from(recorded_size).map_err(PageIoError::unreadable)?;
    // The size is allocated before anything is decompressed, so a corrupt header mustn't be
    // able to ask for a gigabyte. Real pages expand by well under ten times.
    let largest_readable = page.len() * MAX_PAGE_EXPANSION;
    if contents_size > largest_readable {
        return Err(PageIoError::Unreadable(format!(
            "The frame claims {contents_size} bytes, more than the {largest_readable} a page can hold"
        )));
    }
    decompress(frame, contents_size)
}

fn zstd_error(code: usize) -> PageIoError {
    PageIoError::unreadable(zstd_safe::get_error_name(code))
}

fn decode_page<T: DeserializeOwned>(
    page: &[u8],
    term: Option<&str>,
) -> Result<Vec<T>, PageIoError> {
    let contents = decompress_page(page)?;
    let documents: Vec<T> = serde_json::from_slice(&contents).map_err(PageIoError::unreadable)?;
    let Some(term) = term else {
        return Ok(documents);
    };
    // Writers read whole pages, so only readers pay for parsing the page a second time.
    let values: Vec<Value> = serde_json::from_slice(&contents).map_err(PageIoError::unreadable)?;
    Ok(zip(documents, values)
        .filter_map(|(document, value)| has_term(&value, term).then_some(document))
        .collect())
}

/// Whether a document belongs to `term`: its own, or no term at all. Python writes documents
/// as tuples and the crawler as objects, so the term is looked for in either.
fn has_term(document: &Value, term: &str) -> bool {
    let document_term = match document {
        Value::Object(fields) => fields.get("term"),
        _ => document.get(TERM_INDEX),
    };
    match document_term {
        None | Some(Value::Null) => true,
        Some(Value::String(document_term)) => document_term == term,
        Some(_) => false,
    }
}

/// The first `count` of the serialised documents, as a compressed JSON array.
fn compress_prefix(serialised: &[String], count: usize) -> Result<Vec<u8>, PageIoError> {
    let mut json = String::from("[");
    for (position, document) in serialised.iter().take(count).enumerate() {
        if position > 0 {
            json.push(',');
        }
        json.push_str(document);
    }
    json.push(']');
    compress(json.as_bytes())
}

/// The most of `serialised` that compresses to `page_size` bytes or fewer, found by binary
/// search, as a count and the compressed bytes. If even none is too big, it is that.
fn longest_fitting_prefix(
    serialised: &[String],
    page_size: usize,
) -> Result<(usize, Vec<u8>), PageIoError> {
    let mut best = (0, compress_prefix(serialised, 0)?);
    let (mut low, mut high) = (0, serialised.len());
    while low <= high {
        let count = usize::midpoint(low, high);
        let compressed = compress_prefix(serialised, count)?;
        if compressed.len() <= page_size {
            best = (count, compressed);
            low = count + 1;
        } else if let Some(below) = count.checked_sub(1) {
            high = below;
        } else {
            break;
        }
    }
    Ok(best)
}

/// The padded bytes of a page of `page_size` holding as many of `documents` as fit, and how
/// many that is.
///
/// How many fit depends on how well they compress. Those that don't are dropped from the
/// end, so callers pass their documents best first.
///
/// # Errors
///
/// Fails with `Unreadable` if a document won't serialise or even an empty page is too big,
/// and with `Io` if zstd could not make a context or compress.
pub fn pack_page<T: Serialize>(
    page_size: usize,
    documents: &[T],
) -> Result<(Vec<u8>, usize), PageIoError> {
    let serialised = documents
        .iter()
        .map(serde_json::to_string)
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| PageIoError::Unreadable(format!("Failed to serialize data: {error}")))?;
    // Nearly every page holds all of its documents (99% when indexing a crawl), and that
    // takes one compression rather than a search's handful.
    let all = compress_prefix(&serialised, documents.len())?;
    if all.len() <= page_size {
        return Ok(pad_page(all, documents.len(), page_size));
    }
    let (num_stored, page) = longest_fitting_prefix(&serialised, page_size)?;
    if page.len() > page_size {
        return Err(PageIoError::Unreadable(format!(
            "Data is too big ({}) for page size ({page_size})",
            page.len()
        )));
    }
    Ok(pad_page(page, num_stored, page_size))
}

fn pad_page(mut page: Vec<u8>, num_stored: usize, page_size: usize) -> (Vec<u8>, usize) {
    page.resize(page_size, 0);
    (page, num_stored)
}

/// The documents on the page of `page_size` bytes at `offset` in the file open as `fd`, which
/// must stay open for the whole call.
///
/// Pass `term` to keep only the documents that belong to it: those whose term is `term`, or
/// which have none.
///
/// # Errors
///
/// Fails with `Io` if the read fails or zstd could not make a context, and with `Unreadable`
/// if the page comes up short or won't decode, which means it is corrupt or was caught
/// half-written. There is no falling back to an empty page: a writer that merged onto one
/// would store its result over everything that was there.
///
/// # Panics
///
/// If `fd` is -1.
pub fn read_index_page<T: DeserializeOwned>(
    fd: i32,
    offset: u64,
    page_size: usize,
    term: Option<&str>,
) -> Result<Vec<T>, PageIoError> {
    let page = read_page_bytes(fd, offset, page_size)?;
    decode_page(&page, term)
}

/// Writes as many of `documents` as fit over the page at `offset` in the file open as `fd`,
/// returning how many that is. `fd` must stay open for the whole call.
///
/// # Errors
///
/// Fails as [`pack_page`] does, and with `Io` if the write fails.
///
/// # Panics
///
/// If `fd` is -1.
pub fn write_index_page<T: Serialize>(
    fd: i32,
    offset: u64,
    page_size: usize,
    documents: &[T],
) -> Result<usize, PageIoError> {
    let (page, num_stored) = pack_page(page_size, documents)?;
    let file = borrow_file(fd);
    // write_all_at rather than write_at, because a short write would leave the page torn,
    // half old and half new. It also retries a write a signal interrupted.
    file.write_all_at(&page, offset).map_err(PageIoError::Io)?;
    Ok(num_stored)
}

/// The tokens to index a document under, with no repeats and in no particular order: every
/// token of its title, URL and extract, and the bigrams of the first tokens of each.
#[must_use]
pub fn tokenize_document(url: &str, title: &str, extract: &str) -> Vec<String> {
    let token_lists = [
        tokenize(title),
        tokenize(&prepare_url_for_tokenizing(url)),
        tokenize(extract),
    ];
    let bigrams: Vec<String> = token_lists
        .iter()
        .flat_map(|tokens| first_bigrams(tokens))
        .collect();
    let tokens: HashSet<String> = bigrams
        .into_iter()
        .chain(token_lists.into_iter().flatten())
        .collect();
    tokens.into_iter().collect()
}

fn first_bigrams(tokens: &[String]) -> Vec<String> {
    let first_tokens = tokens.get(..NUM_FIRST_TOKENS).unwrap_or(tokens);
    get_bigrams(NUM_FIRST_TOKENS, first_tokens)
}

/// `url` with each character but an ASCII letter or digit replaced by a space, for
/// `tokenize`.
///
/// An `http://` prefix is removed first, but `https://` only when it is followed by
/// `http://`, which goes too: other `https` URLs keep their scheme as a token.
#[must_use]
pub fn prepare_url_for_tokenizing(url: &str) -> String {
    let url = url
        .strip_prefix("https://")
        .unwrap_or(url)
        .strip_prefix("http://")
        .unwrap_or(url);
    url.chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { ' ' })
        .collect()
}
