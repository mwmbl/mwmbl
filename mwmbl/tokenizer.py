def tokenize(input_text):
    cleaned_text = clean_unicode(input_text)
    tokens = cleaned_text.lower().split()
    if input_text.endswith('…'):
        # Discard the last two tokens since there will likely be a word cut in two
        tokens = tokens[:-2]
    return tokens


def get_bigrams(num_bigrams, tokens):
    num_bigrams = min(num_bigrams, len(tokens) - 1)
    bigrams = [f'{tokens[i]} {tokens[i + 1]}' for i in range(num_bigrams)]
    return bigrams


def clean_unicode(s: str) -> str:
    return s.encode('utf-8', errors='ignore').decode('utf-8')
