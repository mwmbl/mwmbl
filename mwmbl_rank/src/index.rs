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

use mwmbl_core::index::{self, PageIoError};
use pyo3::create_exception;
use pyo3::exceptions::{PyException, PyOSError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyList, PyLong, PyString, PyTuple};
use serde_json::{Map, Number, Value};

struct PyPageIoError(PageIoError);

impl From<PageIoError> for PyPageIoError {
    fn from(error: PageIoError) -> PyPageIoError {
        PyPageIoError(error)
    }
}

impl From<PyPageIoError> for PageIoError {
    fn from(error: PyPageIoError) -> PageIoError {
        error.0
    }
}

impl From<PyPageIoError> for PyErr {
    fn from(error: PyPageIoError) -> PyErr {
        match error.0 {
            PageIoError::Io(e) => PyOSError::new_err(e.to_string()),
            PageIoError::Unreadable(message) => PageError::new_err(message),
        }
    }
}

create_exception!(
    mwmbl_rank,
    PageError,
    PyException,
    "A page that will not decode, or data that will not fit on one."
);

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
    let values = py.allow_threads(|| -> Result<Vec<Value>, PyPageIoError> {
        index::read_index_page(fd, offset, page_size, term.as_ref()).map_err(PyPageIoError)
    })?;
    Ok(values_to_py(py, values)?.unbind())
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
    let num_stored = py.allow_threads(|| -> Result<usize, PyPageIoError> {
        index::write_index_page(fd, offset, page_size, &values).map_err(PyPageIoError)
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
    let (page, num_stored) = py
        .allow_threads(|| index::pack_page(page_size, &values).map_err(PyPageIoError))?;
    Ok((PyBytes::new_bound(py, &page).unbind(), num_stored))
}
