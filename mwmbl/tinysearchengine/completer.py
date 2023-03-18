from bisect import bisect_left, bisect_right
from pathlib import Path

import pandas as pd


TERMS_PATH = Path(__file__).parent.parent / 'resources' / 'mwmbl-crawl-terms.csv'


class Completer:
    def __init__(self, num_matches: int = 3):
        # Load term data
        terms = self.get_terms()

        terms_dict = terms.sort_values('term').set_index('term')['count'].to_dict()
        self.terms = list(terms_dict.keys())
        self.counts = list(terms_dict.values())
        self.num_matches = num_matches
        print("Terms", self.terms[:100], self.counts[:100])
        
    def get_terms(self):
        return pd.read_csv(TERMS_PATH)

    def complete(self, term) -> list[str]:
        term_length = len(term)
        start_index = bisect_left(self.terms, term, key=lambda x: x[:term_length])
        end_index = bisect_right(self.terms, term, key=lambda x: x[:term_length])

        matching_terms = zip(self.counts[start_index:end_index], self.terms[start_index:end_index])
        top_terms = sorted(matching_terms, reverse=True)[:self.num_matches]
        print("Top terms, counts", top_terms)
        if not top_terms:
            return []

        counts, terms = zip(*top_terms)
        return list(terms)
