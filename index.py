"""
Create a search index
"""
import gzip
from glob import glob

import justext
from spacy.lang.en import English

from paths import CRAWL_GLOB


def is_content_token(nlp, token):
    lexeme = nlp.vocab[token.orth]
    return lexeme.is_alpha and not token.is_stop


def tokenize(nlp, cleaned_text):
    tokens = nlp.tokenizer(cleaned_text)
    content_tokens = [token for token in tokens if is_content_token(nlp, token)]
    lowered = {nlp.vocab[token.orth].text.lower() for token in content_tokens}
    return lowered


def clean(content):
    text = justext.justext(content, justext.get_stoplist("English"))
    pars = [par.text for par in text if not par.is_boilerplate]
    cleaned_text = ' '.join(pars)
    return cleaned_text


def run():
    nlp = English()
    for path in glob(CRAWL_GLOB):
        with gzip.open(path) as html_file:
            content = html_file.read().decode("utf8")
        cleaned_text = clean(content)
        tokens = tokenize(nlp, cleaned_text)
        print("Tokens", tokens)
        break


if __name__ == '__main__':
    run()
