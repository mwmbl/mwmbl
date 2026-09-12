//! Matching a user agent against the crawler-user-agents patterns.
//!
//! One question, asked on every search request: does this user agent say it is a crawler?
//! Python's `re` cannot be asked it directly - `'|'.join(the 1500 patterns)` backtracks
//! through every branch and measures 4.5 ms per *non-matching* user agent, which is the
//! common case - so the alternative here was a hand-built trie of the literal patterns.
//!
//! `RegexSet` compiles the whole set into one automaton with a literal prefilter and
//! matches in a single linear pass, measured at 0.5 µs per call from Python including the
//! crossing. A set rather than one joined pattern, at about twice the per-call cost:
//! joining changes what a pattern means if one of them contains a top-level alternation,
//! and these patterns are maintained elsewhere and refreshed by a script.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use regex::RegexSet;

/// Matches a user agent against a fixed set of crawler patterns.
///
/// Compiled once and shared, so the cost is paid at import rather than per request.
/// Immutable after construction, which is what makes it safe to call from every Django
/// worker thread at once.
#[pyclass(name = "BotUserAgentMatcher", frozen)]
pub struct PyBotUserAgentMatcher {
    patterns: RegexSet,
}

#[pymethods]
impl PyBotUserAgentMatcher {
    /// Compile the patterns, raising ValueError if one will not compile.
    ///
    /// Case-sensitive, deliberately: upstream encodes case itself (`[wW]get`,
    /// `S[eE][mM]rushBot`), so folding it would be both looser and slower.
    #[new]
    fn new(patterns: Vec<String>) -> PyResult<Self> {
        let patterns = RegexSet::new(&patterns)
            .map_err(|e| PyValueError::new_err(format!("Could not compile crawler patterns: {e}")))?;
        Ok(PyBotUserAgentMatcher { patterns })
    }

    /// Whether any pattern matches anywhere in the user agent.
    fn is_match(&self, user_agent: &str) -> bool {
        self.patterns.is_match(user_agent)
    }
}
