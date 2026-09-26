def tokenize(input_text):
    cleaned_text = clean_unicode(input_text)
    tokens = cleaned_text.lower().split()
    if input_text.endswith("…"):
        # Discard the last two tokens since there will likely be a word cut in two
        tokens = tokens[:-2]
    return tokens


def get_bigrams(num_bigrams, tokens):
    num_bigrams = min(num_bigrams, len(tokens) - 1)
    bigrams = [f"{tokens[i]} {tokens[i + 1]}" for i in range(num_bigrams)]
    return bigrams


def get_compounds(tokens):
    """The forms a run of query terms takes when a page writes it as one word.

    URL slugs and domains are tokenized whole, so a page can be filed under
    "stocks-and-shares-isa" or "britishmuseum" long after it has been pushed off the
    pages for "stocks" or "british". Adjacent pairs and the whole query cover nearly all
    of these while keeping the lookups linear in the query length.
    """
    runs = [tokens[i : i + 2] for i in range(len(tokens) - 1)]
    if len(tokens) > 2:
        runs.append(tokens)
    return {separator.join(run) for run in runs for separator in ("-", "")}


def clean_unicode(s: str) -> str:
    return s.encode("utf-8", errors="ignore").decode("utf-8")
