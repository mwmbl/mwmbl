//! TinyIndex page I/O.
//!
//! A page of the index is a zstd frame holding a JSON array of document tuples, padded
//! with zeros to the page size. Reading one is a positioned read, a decompress and a
//! parse, and writing one is a serialise, a compress and a positioned write. All of that
//! happens here with the GIL released, so worker threads overlap their page reads instead
//! of queueing for the interpreter - which is what a pread in Python cannot do, because it
//! releases the GIL for the syscall and then has to win it back for the decompress and the
//! parse.
//!
//! Python keeps everything else: it opens the file and passes the descriptor in, holds the
//! metadata and the page locks, and turns the tuples that come back into Documents.

use std::cell::RefCell;
use std::fs::File;
use std::mem::ManuallyDrop;
use std::os::unix::fs::FileExt;
use std::os::unix::io::FromRawFd;

use pyo3::create_exception;
use pyo3::exceptions::{PyException, PyOSError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyList, PyLong, PyString, PyTuple};
use serde_json::{Map, Number, Value};
use zstd::bulk::{Compressor, Decompressor};
use zstd::zstd_safe;

create_exception!(
    mwmbl_rank,
    PageError,
    PyException,
    "A page that will not decode, or data that will not fit on one."
);

/// Where `term` sits in Document.as_tuple(). as_tuple truncates trailing Nones, so a
/// document with no term may have nothing at that position at all.
const TERM_INDEX: usize = 4;

/// python-zstandard's default level, which is what wrote every page in the index today.
const COMPRESSION_LEVEL: i32 = 3;

/// How much bigger than the page itself its contents are allowed to claim to be. See
/// decompress_page: it bounds what a corrupt frame header can make this allocate.
const MAX_PAGE_EXPANSION: usize = 1024;

/// Everything that can go wrong away from the GIL, split by what Python should see: a
/// failed syscall is an OSError as it has always been, and a page that will not decode is
/// a PageError, which every caller of the index already handles.
enum PageIoError {
    Io(std::io::Error),
    Unreadable(String),
}

impl From<PageIoError> for PyErr {
    fn from(error: PageIoError) -> PyErr {
        match error {
            PageIoError::Io(e) => PyOSError::new_err(e.to_string()),
            PageIoError::Unreadable(message) => PageError::new_err(message),
        }
    }
}

/// The descriptor belongs to the Python file object that opened it, so wrap it without
/// taking ownership - a plain File would close it when this returns.
fn borrow_file(fd: i32) -> ManuallyDrop<File> {
    ManuallyDrop::new(unsafe { File::from_raw_fd(fd) })
}

/// A read that came up short is a page that is not there rather than a failed syscall: a
/// positioned read stops at the end of the file, so it means the file is not the shape the
/// metadata says it is - truncated, or half-written by create. Decoding the fragment anyway
/// is how a writer comes to merge onto a page it never actually read.
fn read_error(error: std::io::Error) -> PageIoError {
    if error.kind() == std::io::ErrorKind::UnexpectedEof {
        return PageIoError::Unreadable(format!("could not read a whole page: {error}"));
    }
    PageIoError::Io(error)
}

fn read_page_bytes(fd: i32, offset: u64, page_size: usize) -> Result<Vec<u8>, PageIoError> {
    let file = borrow_file(fd);
    let mut page = vec![0u8; page_size];
    // read_exact_at rather than read_at: it reads until it has the whole page, and it
    // retries a read the kernel interrupted with a signal, which is what os.pread does
    // since PEP 475 and what a bare read_at does not.
    file.read_exact_at(&mut page, offset).map_err(read_error)?;
    Ok(page)
}

thread_local! {
    /// One decompression context per thread, reused for every page it reads. Building one
    /// allocates a working buffer and costs about as much as decompressing a page, and
    /// each call decompresses a whole frame on its own, so there is no state to carry
    /// between reads and a failed page cannot poison the next one.
    static DECOMPRESSOR: RefCell<Decompressor<'static>> =
        RefCell::new(Decompressor::new().expect("Could not create a zstd decompression context"));

    /// And one compression context, for the same reason. Packing a page compresses about
    /// a dozen prefixes of the documents to find the ones that fit, so building a context
    /// per compression cost more than the compression did.
    static COMPRESSOR: RefCell<Compressor<'static>> = RefCell::new(
        Compressor::new(COMPRESSION_LEVEL).expect("Could not create a zstd compression context"));
}

/// Decompress the one zstd frame at the start of the page, ignoring whatever follows it -
/// which is the zero padding that brings the page up to its fixed size.
fn decompress_page(page: &[u8]) -> Result<Vec<u8>, PageIoError> {
    let unreadable = |e: std::io::Error| PageIoError::Unreadable(e.to_string());
    let frame_size = zstd_safe::find_frame_compressed_size(page).map_err(zstd_error)?;
    let frame = &page[..frame_size];
    // Every frame the index writes carries its content size, so this is the exact size of
    // the buffer to decompress into rather than a guess.
    let contents_size = zstd_safe::get_frame_content_size(frame)
        .map_err(|e| PageIoError::Unreadable(e.to_string()))?
        .ok_or_else(|| PageIoError::Unreadable("The frame has no content size".to_string()))?
        as usize;
    // That size is data like anything else on the page, and the buffer is allocated before
    // a byte is decompressed, so a corrupt header must not be able to ask for a gigabyte.
    // Real pages expand by well under ten times; this is far past that and still bounded.
    let largest_readable = page.len() * MAX_PAGE_EXPANSION;
    if contents_size > largest_readable {
        return Err(PageIoError::Unreadable(format!(
            "The frame claims {contents_size} bytes, more than the {largest_readable} a page can hold"
        )));
    }
    DECOMPRESSOR.with_borrow_mut(|decompressor| {
        decompressor
            .decompress(frame, contents_size)
            .map_err(unreadable)
    })
}

/// zstd reports a failure as an error code, and the text for it is all there is to say.
fn zstd_error(code: usize) -> PageIoError {
    PageIoError::Unreadable(zstd_safe::get_error_name(code).to_string())
}

fn decode_page(page: &[u8], term: Option<&str>) -> Result<Vec<Value>, PageIoError> {
    let contents = decompress_page(page)?;
    // Damage that gets past zstd lands on the parse instead, and it is the same kind of
    // unreadable. Invalid UTF-8 fails here too, as the decode in Python did.
    let items: Vec<Value> =
        serde_json::from_slice(&contents).map_err(|e| PageIoError::Unreadable(e.to_string()))?;
    match term {
        None => Ok(items),
        Some(term) => Ok(items
            .into_iter()
            .filter(|item| has_term(item, term))
            .collect()),
    }
}

/// Whether a document tuple belongs to this term: its own, or no term at all. Filtering
/// here rather than in Python means a page's documents that another term put there are
/// never built into Python objects.
fn has_term(item: &Value, term: &str) -> bool {
    match item.get(TERM_INDEX) {
        None | Some(Value::Null) => true,
        Some(Value::String(item_term)) => item_term == term,
        Some(_) => false,
    }
}

fn value_to_py(py: Python<'_>, value: Value) -> PyResult<PyObject> {
    let object = match value {
        Value::Null => py.None(),
        Value::Bool(boolean) => boolean.into_py(py),
        Value::Number(number) => match (number.as_i64(), number.as_u64(), number.as_f64()) {
            (Some(integer), _, _) => integer.into_py(py),
            (_, Some(unsigned), _) => unsigned.into_py(py),
            (_, _, Some(float)) => float.into_py(py),
            // A JSON number is an i64, a u64 or an f64, so nothing reaches this.
            _ => return Err(PageError::new_err(format!("Unreadable number {number}"))),
        },
        Value::String(string) => string.into_py(py),
        Value::Array(items) => values_to_py(py, items)?.into(),
        Value::Object(fields) => {
            let dict = PyDict::new_bound(py);
            for (key, field) in fields {
                dict.set_item(key, value_to_py(py, field)?)?;
            }
            dict.into()
        }
    };
    Ok(object)
}

/// A JSON array as the list json.loads would have returned.
fn values_to_py<'py>(py: Python<'py>, values: Vec<Value>) -> PyResult<Bound<'py, PyList>> {
    let items = values
        .into_iter()
        .map(|value| value_to_py(py, value))
        .collect::<PyResult<Vec<PyObject>>>()?;
    Ok(PyList::new_bound(py, items))
}

fn py_to_value(object: &Bound<'_, PyAny>) -> PyResult<Value> {
    if object.is_none() {
        return Ok(Value::Null);
    }
    // Before the integer check, not after: a Python bool is an int.
    if let Ok(boolean) = object.downcast::<PyBool>() {
        return Ok(Value::Bool(boolean.is_true()));
    }
    if let Ok(integer) = object.downcast::<PyLong>() {
        return Ok(Value::Number(Number::from(integer.extract::<i64>()?)));
    }
    if let Ok(float) = object.downcast::<PyFloat>() {
        let value = float.value();
        // JSON has no NaN or infinity. Python's json module writes them anyway, as words
        // no other reader accepts, so a score that has gone wrong is caught here rather
        // than written to a page that will not parse again.
        let number = Number::from_f64(value).ok_or_else(|| {
            PyValueError::new_err(format!("{value} cannot be stored in an index page"))
        })?;
        return Ok(Value::Number(number));
    }
    if let Ok(string) = object.downcast::<PyString>() {
        return Ok(Value::String(string.to_str()?.to_owned()));
    }
    if let Ok(list) = object.downcast::<PyList>() {
        return Ok(Value::Array(
            list.iter()
                .map(|item| py_to_value(&item))
                .collect::<PyResult<_>>()?,
        ));
    }
    if let Ok(tuple) = object.downcast::<PyTuple>() {
        return Ok(Value::Array(
            tuple
                .iter()
                .map(|item| py_to_value(&item))
                .collect::<PyResult<_>>()?,
        ));
    }
    if let Ok(dict) = object.downcast::<PyDict>() {
        let mut fields = Map::with_capacity(dict.len());
        for (key, value) in dict.iter() {
            let name = key.downcast::<PyString>().map_err(|_| {
                PyTypeError::new_err("Only string keys can be stored in an index page")
            })?;
            fields.insert(name.to_str()?.to_owned(), py_to_value(&value)?);
        }
        return Ok(Value::Object(fields));
    }
    Err(PyTypeError::new_err(format!(
        "Cannot store {} in an index page",
        object.get_type().name()?
    )))
}

/// The items to store, as JSON values.
///
/// Anything that will not convert is a PageError rather than the TypeError or ValueError it
/// arrives as: a score that has come out NaN, a string holding an unpaired surrogate, a
/// type with no JSON form. json.dumps wrote all three without complaint, so they can reach
/// here, and a caller that catches PageError loses the one page it could not write. Letting
/// the raw exception out instead would abort the whole run - see index_pages, which catches
/// PageError for exactly that reason.
fn py_items_to_values(items: &Bound<'_, PyList>) -> PyResult<Vec<Value>> {
    items
        .iter()
        .map(|item| py_to_value(&item))
        .collect::<PyResult<Vec<Value>>>()
        .map_err(|e| PageError::new_err(format!("Cannot store these documents on a page: {e}")))
}

fn compress_prefix(serialised: &[String], count: usize) -> Result<Vec<u8>, PageIoError> {
    let mut json = String::from("[");
    for (position, item) in serialised[..count].iter().enumerate() {
        if position > 0 {
            json.push(',');
        }
        json.push_str(item);
    }
    json.push(']');
    COMPRESSOR.with_borrow_mut(|compressor| {
        compressor
            .compress(json.as_bytes())
            .map_err(PageIoError::Io)
    })
}

/// The padded page bytes, and how many of `items` actually fit on them.
///
/// A page holds a fixed number of bytes, and how many documents that is depends on how
/// well they compress, so the largest prefix that fits is found by binary search over the
/// compressed size. The tail that does not fit is dropped, which is why callers pass their
/// documents best-first.
fn pack_page(page_size: usize, items: &[Value]) -> Result<(Vec<u8>, usize), PageIoError> {
    let serialised: Vec<String> = items.iter().map(|item| item.to_string()).collect();
    // One compression context for the whole search: the binary search compresses a dozen
    // prefixes, and building a context each time would cost more than the compression.
    let mut low = 0;
    let mut high = items.len();
    let mut best = (0, compress_prefix(&serialised, 0)?);
    while low <= high {
        let count = (low + high) / 2;
        let compressed = compress_prefix(&serialised, count)?;
        if compressed.len() > page_size {
            if count == 0 {
                break;
            }
            high = count - 1;
        } else {
            best = (count, compressed);
            low = count + 1;
        }
    }

    let (num_stored, mut page) = best;
    if page.len() > page_size {
        return Err(PageIoError::Unreadable(format!(
            "Data is too big ({}) for page size ({page_size})",
            page.len()
        )));
    }
    page.resize(page_size, 0);
    Ok((page, num_stored))
}

/// Read the page at `offset`, returning its document tuples as lists.
///
/// Pass `term` to keep only the documents that belong to it - the ones whose term is that
/// term, or which have none. Raises PageError if the page will not decode, which means it
/// is corrupt or was caught half-written; it deliberately does not fall back to an empty
/// page, because a writer that merged onto one would store its result over everything that
/// was there.
#[pyfunction]
#[pyo3(signature = (fd, offset, page_size, term=None))]
pub fn read_index_page(
    py: Python<'_>,
    fd: i32,
    offset: u64,
    page_size: usize,
    term: Option<String>,
) -> PyResult<Py<PyList>> {
    let items = py.allow_threads(|| {
        let page = read_page_bytes(fd, offset, page_size)?;
        decode_page(&page, term.as_deref())
    })?;
    Ok(values_to_py(py, items)?.unbind())
}

/// Write `items` over the page at `offset`, returning how many of them fitted.
#[pyfunction]
pub fn write_index_page(
    py: Python<'_>,
    fd: i32,
    offset: u64,
    page_size: usize,
    items: &Bound<'_, PyList>,
) -> PyResult<usize> {
    let values = py_items_to_values(items)?;
    let num_stored = py.allow_threads(|| -> Result<usize, PageIoError> {
        let (page, num_stored) = pack_page(page_size, &values)?;
        let file = borrow_file(fd);
        // write_all_at rather than write_at: a short write would leave the page half old
        // and half new, which is the torn page every reader and writer here is guarding
        // against, so it writes the rest rather than reporting it - and, like the read, it
        // retries a write a signal interrupted. What is left is a real failure of the
        // device, which is an OSError as it has always been.
        file.write_all_at(&page, offset).map_err(PageIoError::Io)?;
        Ok(num_stored)
    })?;
    Ok(num_stored)
}

/// The bytes of a page holding `items`, and how many of them fitted, without writing it.
/// TinyIndex.create uses this to lay down the empty pages of a new index.
#[pyfunction]
pub fn pack_index_page(
    py: Python<'_>,
    page_size: usize,
    items: &Bound<'_, PyList>,
) -> PyResult<(Py<PyBytes>, usize)> {
    let values = py_items_to_values(items)?;
    let (page, num_stored) = py.allow_threads(|| pack_page(page_size, &values))?;
    Ok((PyBytes::new_bound(py, &page).unbind(), num_stored))
}
