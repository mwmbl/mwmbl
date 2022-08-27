def tokenize(input_text):
    cleaned_text = input_text.encode('utf8', 'replace').decode('utf8')
    tokens = cleaned_text.lower().split()
    if input_text.endswith('…'):
        # Discard the last two tokens since there will likely be a word cut in two
        tokens = tokens[:-2]
    return tokens


def get_bigrams(num_bigrams, tokens):
    num_bigrams = min(num_bigrams, len(tokens) - 1)
    bigrams = [f'{tokens[i]} {tokens[i + 1]}' for i in range(num_bigrams)]
    return bigrams
