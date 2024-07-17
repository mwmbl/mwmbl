"""
Estimate word and document frequencies from a sample of the index.
"""
import json
import os
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

from mwmbl.indexer.index import prepare_url_for_tokenizing
from mwmbl.tinysearchengine.indexer import Document, TinyIndex
from mwmbl.tokenizer import tokenize

INDEX_SAMPLE_PATH = Path(os.environ["HOME"]) / "index-sample.tinysearch"
DOCUMENT_COUNTS_PATH = Path(__file__).parent.parent / "mwmbl" / "resources" / "document_counts.json"


def get_frequencies():
    document_counts = Counter()
    with TinyIndex(item_factory=Document, index_path=INDEX_SAMPLE_PATH) as tiny_index:
        for i in range(9999):
            page = tiny_index.get_page(i)
            for document in page:
                prepared_url = prepare_url_for_tokenizing(unquote(document.url))
                for s in [document.title, prepared_url, document.extract]:
                    tokens = tokenize(s)
                    document_counts.update(set(tokens))

    print("\nDocument frequencies")
    for term, count in document_counts.most_common(200):
        print(f"{term}: {count}")

    with open(DOCUMENT_COUNTS_PATH, 'w') as f:
        json.dump(document_counts, f, indent=2)


if __name__ == '__main__':
    get_frequencies()
