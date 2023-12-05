import re

from django.template import Library
from django.utils.html import conditional_escape
from django.utils.safestring import mark_safe

from mwmbl.format import get_query_regex, DOCUMENT_SOURCES, get_document_source
from mwmbl.tinysearchengine.indexer import DocumentState
from mwmbl.tokenizer import tokenize

register = Library()


@register.filter(needs_autoescape=True)
def format_for_query(text: str, query: str, autoescape=True):
    escape = conditional_escape if autoescape else lambda x: x
    tokens = tokenize(query)
    pattern = get_query_regex(tokens, True, False)
    matches = re.finditer(pattern, text, re.IGNORECASE)
    formatted = []
    start = 0
    for match in matches:
        formatted.append(escape(text[start:match.start()]))
        formatted.append(f"<strong>{escape(text[match.start():match.end()])}</strong>")
        start = match.end()
    formatted.append(escape(text[start:]))
    return mark_safe("".join(formatted))


@register.filter(needs_autoescape=True)
def convert_state_to_source(state: DocumentState, autoescape=True):
    escape = conditional_escape if autoescape else lambda x: x
    return escape(get_document_source(state))
