import re

from mwmbl.tokenizer import tokenize


def format_result_with_pattern(pattern, result):
    formatted_result = {}
    for content_type, content in [('title', result.title), ('extract', result.extract)]:
        matches = re.finditer(pattern, content, re.IGNORECASE)
        all_spans = [0] + sum((list(m.span()) for m in matches), []) + [len(content)]
        content_result = []
        for i in range(len(all_spans) - 1):
            is_bold = i % 2 == 1
            start = all_spans[i]
            end = all_spans[i + 1]
            content_result.append({'value': content[start:end], 'is_bold': is_bold})
        formatted_result[content_type] = content_result
    formatted_result['url'] = result.url
    return formatted_result


def get_query_regex(terms, is_complete, is_url):
    if not terms:
        return ''

    word_sep = r'\b' if is_url else ''
    if is_complete:
        term_patterns = [rf'{word_sep}{re.escape(term)}{word_sep}' for term in terms]
    else:
        term_patterns = [rf'{word_sep}{re.escape(term)}{word_sep}' for term in terms[:-1]] + [
            rf'{word_sep}{re.escape(terms[-1])}']
    pattern = '|'.join(term_patterns)
    return pattern


def format_result(result, query):
    tokens = tokenize(query)
    pattern = get_query_regex(tokens, True, False)
    return format_result_with_pattern(pattern, result)

