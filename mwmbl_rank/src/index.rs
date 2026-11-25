//! Reading and writing `TinyIndex` pages for Python.
//!
//! A page is a zstd frame holding a JSON array of documents, padded with zeros to the page
//! size. The I/O, the compression and the parse all happen with the GIL released, so worker
//! threads overlap their page reads. A pread in Python can't do that: it releases the GIL for
//! the syscall but has to win it back for the decompress and the parse.
//!
//! Python opens the file and passes the descriptor in, holds the metadata and the page locks,
//! and turns the documents that come back into `Document`s.

use std::fs::File;
use std::mem::ManuallyDrop;
use std::os::unix::io::FromRawFd;

use anyhow::{Context, Result};
use mwmbl_core::index::{self, PageIoError};
use pyo3::create_exception;
use pyo3::exceptions::{PyException, PyOSError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyList, PyLong, PyString, PyTuple};
use serde_json::{Map, Number, Value};

create_exception!(
    mwmbl_rank,
    PageError,
    PyException,
    "A page that will not decode, or data that will not fit on one."
);

/// The file Python has open as `fd`, borrowed for one call. Python owns it, so it must not be
/// closed: a `File` would close it on drop, and the `ManuallyDrop` stops that.
///
/// # Panics
///
/// If `fd` is -1, which `File::from_raw_fd` asserts against.
fn borrow_python_file(fd: i32) -> ManuallyDrop<File> {
    // SAFETY: Python keeps the file open across the call, and the ManuallyDrop never closes
    // it. Python hands over only an integer, so this is the one place a raw descriptor
    // becomes a File.
    ManuallyDrop::new(unsafe { File::from_raw_fd(fd) })
}

/// A page I/O error as a Python `OSError`, or a `PageError` for a page that won't decode or
/// data that won't fit.
fn page_io_error(error: PageIoError) -> PyErr {
    match error {
        PageIoError::Io(error) => PyOSError::new_err(error.to_string()),
        PageIoError::Unreadable(message) => PageError::new_err(message),
    }
}

/// A conversion error as a Python `PageError`, with its context chain in the message.
fn page_error(error: &anyhow::Error) -> PyErr {
    PageError::new_err(format!("{error:#}"))
}

/// A JSON value as the object `json.loads` would have returned.
fn value_to_py(py: Python<'_>, value: Value) -> PyResult<PyObject> {
    let object = match value {
        Value::Null => py.None(),
        Value::Bool(boolean) => boolean.into_py(py),
        Value::Number(number) => {
            if let Some(integer) = number.as_i64() {
                integer.into_py(py)
            } else if let Some(unsigned) = number.as_u64() {
                unsigned.into_py(py)
            } else if let Some(float) = number.as_f64() {
                float.into_py(py)
            } else {
                // Without serde_json's arbitrary_precision, every number is one of the three.
                return Err(PageError::new_err(format!("Unreadable number {number}")));
            }
        }
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

/// A JSON array as the list `json.loads` would have returned.
fn values_to_py(py: Python<'_>, values: Vec<Value>) -> PyResult<Bound<'_, PyList>> {
    let items = values
        .into_iter()
        .map(|value| value_to_py(py, value))
        .collect::<PyResult<Vec<PyObject>>>()?;
    Ok(PyList::new_bound(py, items))
}

/// A Python object as a JSON value.
fn py_to_value(object: &Bound<'_, PyAny>) -> PyResult<Value> {
    if object.is_none() {
        return Ok(Value::Null);
    }
    // A Python bool is an int, so it is checked first.
    if let Ok(boolean) = object.downcast::<PyBool>() {
        return Ok(Value::Bool(boolean.is_true()));
    }
    if let Ok(integer) = object.downcast::<PyLong>() {
        return Ok(Value::Number(Number::from(integer.extract::<i64>()?)));
    }
    if let Ok(float) = object.downcast::<PyFloat>() {
        let value = float.value();
        // Python's json module writes NaN and infinity as words no JSON reader accepts, so
        // a score that has gone wrong is caught here rather than written to a page that
        // won't parse again.
        let Some(number) = Number::from_f64(value) else {
            return Err(PyValueError::new_err(format!(
                "{value} cannot be stored in an index page"
            )));
        };
        return Ok(Value::Number(number));
    }
    if let Ok(string) = object.downcast::<PyString>() {
        return Ok(Value::String(string.to_str()?.to_owned()));
    }
    if let Ok(list) = object.downcast::<PyList>() {
        let items = list.iter().map(|item| py_to_value(&item));
        return Ok(Value::Array(items.collect::<PyResult<_>>()?));
    }
    if let Ok(tuple) = object.downcast::<PyTuple>() {
        let items = tuple.iter().map(|item| py_to_value(&item));
        return Ok(Value::Array(items.collect::<PyResult<_>>()?));
    }
    if let Ok(dict) = object.downcast::<PyDict>() {
        let mut fields = Map::with_capacity(dict.len());
        for (key, value) in dict {
            let Ok(name) = key.downcast::<PyString>() else {
                return Err(PyTypeError::new_err(
                    "Only string keys can be stored in an index page",
                ));
            };
            fields.insert(name.to_str()?.to_owned(), py_to_value(&value)?);
        }
        return Ok(Value::Object(fields));
    }
    let object_type = object.get_type();
    let type_name = object_type.name()?;
    Err(PyTypeError::new_err(format!(
        "Cannot store {type_name} in an index page"
    )))
}

/// The items to store, as JSON values.
///
/// # Errors
///
/// Fails if an item has no JSON form, such as a NaN score, a string holding an unpaired
/// surrogate or a set.
fn py_items_to_values(items: &Bound<'_, PyList>) -> Result<Vec<Value>> {
    items
        .iter()
        .map(|item| py_to_value(&item))
        .collect::<PyResult<_>>()
        .context("Cannot store these documents on a page")
}

/// Read the page at `offset`, returning its documents as lists, or as dicts for those the
/// crawler wrote.
///
/// Pass `term` to keep only the documents that belong to it: those whose term is `term`, or
/// which have none.
///
/// # Errors
///
/// Raises `OSError` if the read fails, and `PageError` if the page comes up short or won't
/// decode, which means it is corrupt or was caught half-written. It doesn't fall back to an
/// empty page, because a writer that merged onto one would store its result over everything
/// that was there.
#[pyfunction]
#[pyo3(signature = (fd, offset, page_size, term=None))]
pub fn read_index_page(
    py: Python<'_>,
    fd: i32,
    offset: u64,
    page_size: usize,
    term: Option<&str>,
) -> PyResult<Py<PyList>> {
    let file = borrow_python_file(fd);
    let values = py
        .allow_threads(|| index::read_index_page::<Value>(&file, offset, page_size, term))
        .map_err(page_io_error)?;
    Ok(values_to_py(py, values)?.unbind())
}

/// Write `items` over the page at `offset`, returning how many of them fitted.
///
/// # Errors
///
/// Raises `PageError` if an item can't be stored, rather than the Python exception
/// behind it, so that a caller catching `PageError` loses only this page. Also raises
/// `PageError` if `page_size` is too small for an empty page, and `OSError` if the write fails.
#[pyfunction]
pub fn write_index_page(
    py: Python<'_>,
    fd: i32,
    offset: u64,
    page_size: usize,
    items: &Bound<'_, PyList>,
) -> PyResult<usize> {
    let values = py_items_to_values(items).map_err(|error| page_error(&error))?;
    let file = borrow_python_file(fd);
    py.allow_threads(|| index::write_index_page(&file, offset, page_size, &values))
        .map_err(page_io_error)
}

/// The bytes of a page holding `items`, and how many of them fitted, without writing it.
/// `TinyIndex.create` uses this to lay down the empty pages of a new index.
///
/// # Errors
///
/// Raises `PageError` as `write_index_page` does.
#[pyfunction]
pub fn pack_index_page(
    py: Python<'_>,
    page_size: usize,
    items: &Bound<'_, PyList>,
) -> PyResult<(Py<PyBytes>, usize)> {
    let values = py_items_to_values(items).map_err(|error| page_error(&error))?;
    let (page, num_stored) = py
        .allow_threads(|| index::pack_page(page_size, &values))
        .map_err(page_io_error)?;
    Ok((PyBytes::new_bound(py, &page).unbind(), num_stored))
}
