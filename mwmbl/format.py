import re

from mwmbl.tinysearchengine.indexer import DocumentSource, DocumentState
from mwmbl.tokenizer import clean_unicode, tokenize

DOCUMENT_SOURCES = {
    DocumentState.FROM_GOOGLE: "google",
    DocumentState.FROM_USER: "user",
    DocumentState.FROM_WIKI: "wikipedia",
    DocumentState.ORGANIC_APPROVED: "mwmbl",
    DocumentState.FROM_GOOGLE_APPROVED: "google",
    DocumentState.FROM_USER_APPROVED: "user",
    DocumentState.FROM_WIKI_APPROVED: "wikipedia",
}

# What each external provider is called on the wire. DocumentSource is append-only and every
# member must appear here, or a result from a new provider cannot be named - test_format
# asserts the map is complete rather than leaving that to a KeyError in a response.
DOCUMENT_PROVIDERS = {
    DocumentSource.WIKIPEDIA: "wikipedia",
    DocumentSource.STAAN: "staan",
}


HIGHLIGHT_STOPWORDS = {
    # Articles & Determiners
    "a",
    "an",
    "the",
    "this",
    "that",
    "these",
    "those",
    "each",
    "every",
    "some",
    "any",
    # Prepositions
    "to",
    "in",
    "on",
    "at",
    "by",
    "for",
    "with",
    "about",
    "against",
    "between",
    "into",
    "through",
    "during",
    "before",
    "after",
    "above",
    "below",
    "from",
    "up",
    "down",
    "of",
    "off",
    "over",
    "under",
    # Conjunctions
    "and",
    "but",
    "or",
    "nor",
    "for",
    "yet",
    "so",
    "although",
    "because",
    "since",
    "unless",
    # Common Verbs & Pronouns
    "is",
    "am",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "have",
    "has",
    "had",
    "do",
    "does",
    "did",
    "i",
    "me",
    "my",
    "you",
    "your",
    "he",
    "him",
    "his",
    "she",
    "her",
    "it",
    "its",
    "we",
    "us",
    "our",
    "they",
    "them",
    "their",
    # Interrogatives (usually noise in technical queries)
    "how",
    "what",
    "which",
    "who",
    "whom",
    "where",
    "when",
    "why",
}


def get_document_source(state: DocumentState):
    return DOCUMENT_SOURCES.get(state, "mwmbl")


def get_result_source(result):
    """Where a result came from, for the API's `source` / `engine` field.

    DocumentSource names the external provider a document was fetched from, so it is the more
    specific answer and wins when set; DocumentState answers for everything that came out of
    our own index. Without this a Staan result would report as "mwmbl": it has no
    DocumentState of its own, because it never enters the index.
    """
    if result.source is not None:
        return DOCUMENT_PROVIDERS[result.source]
    return get_document_source(result.state)


def format_result_with_pattern(pattern, result):
    formatted_result = {}
    for content_type, content_raw in [("title", result.title), ("extract", result.extract)]:
        content = clean_unicode(content_raw) if content_raw else ""
        matches = re.finditer(pattern, content, re.IGNORECASE)
        all_spans = [0] + sum((list(m.span()) for m in matches), []) + [len(content)]
        content_result = []
        for i in range(len(all_spans) - 1):
            is_bold = i % 2 == 1
            start = all_spans[i]
            end = all_spans[i + 1]
            if end - start > 0:
                content_result.append({"value": content[start:end], "is_bold": is_bold})
        formatted_result[content_type] = content_result
    formatted_result["url"] = result.url
    formatted_result["source"] = get_result_source(result)
    return formatted_result


def get_query_regex(terms, is_complete: bool, use_word_boundaries: bool):
    if not terms:
        return ""

    word_sep = r"\b" if use_word_boundaries else ""
    if is_complete:
        term_patterns = [rf"{word_sep}{re.escape(term)}{word_sep}" for term in terms]
    else:
        term_patterns = [rf"{word_sep}{re.escape(term)}{word_sep}" for term in terms[:-1]] + [
            rf"{word_sep}{re.escape(terms[-1])}"
        ]
    pattern = "|".join(term_patterns)
    return pattern


def format_result(result, query):
    tokens = tokenize(query)
    filtered_tokens = [t for t in tokens if t not in HIGHLIGHT_STOPWORDS]
    pattern = get_query_regex(filtered_tokens, True, True)
    return format_result_with_pattern(pattern, result)


def _extract_highlights(segments: list[dict]) -> list[str]:
    """Merge consecutive bold segments (with whitespace-only gaps) into phrases."""
    phrases = []
    current: list[str] = []
    for seg in segments:
        if seg["is_bold"]:
            current.append(seg["value"])
        elif current and not seg["value"].strip():
            current.append(seg["value"])
        else:
            if current:
                phrases.append("".join(current).strip())
                current = []
    if current:
        phrases.append("".join(current).strip())

    unique = set(phrases)
    return sorted(unique, key=len, reverse=True)


def format_result_v2(result, position: int, query: str) -> dict:
    tokens = tokenize(query)
    filtered_tokens = [t for t in tokens if t not in HIGHLIGHT_STOPWORDS]
    pattern = get_query_regex(filtered_tokens, True, True)
    v1 = format_result_with_pattern(pattern, result)
    title = "".join(seg["value"] for seg in v1["title"])
    content = "".join(seg["value"] for seg in v1["extract"])
    return {
        "url": result.url,
        "title": title,
        "title_highlights": _extract_highlights(v1["title"]),
        "content": content,
        "content_highlights": _extract_highlights(v1["extract"]),
        "engine": get_result_source(result),
        "score": 1.0 / position,
    }
