# Sentence punctuation that sticks to a word, as in "(see" or "wikipedia,". Stripping it
# lets the word match a query. Only these characters are stripped, so "c++", "c#",
# "--global" and ".net" keep theirs, and a token that is nothing but punctuation, like
# "-", is kept whole.
LEADING_PUNCTUATION = "([{\"'“‘«"
TRAILING_PUNCTUATION = ".,;:!?)]}\"'”’»"


def tokenize(input_text):
    cleaned_text = clean_unicode(input_text)
    tokens = [strip_punctuation(token) for token in cleaned_text.lower().split()]
    if input_text.endswith("…"):
        # Discard the last two tokens since there will likely be a word cut in two
        tokens = tokens[:-2]
    return tokens


def strip_punctuation(token: str) -> str:
    stripped = token.lstrip(LEADING_PUNCTUATION).rstrip(TRAILING_PUNCTUATION)
    return stripped or token


def get_bigrams(num_bigrams, tokens):
    num_bigrams = min(num_bigrams, len(tokens) - 1)
    bigrams = [f"{tokens[i]} {tokens[i + 1]}" for i in range(num_bigrams)]
    return bigrams


def clean_unicode(s: str) -> str:
    return s.encode("utf-8", errors="ignore").decode("utf-8")
