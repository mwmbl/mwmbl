from bisect import bisect_left, bisect_right
from datetime import datetime

import pandas as pd
from pandas import DataFrame


class Completer:
    def __init__(self, terms: DataFrame, num_matches: int = 3):
        terms_dict = terms.sort_values('term').set_index('term')['count'].to_dict()
        self.terms = list(terms_dict.keys())
        self.counts = list(terms_dict.values())
        self.num_matches = num_matches
        print("Terms", self.terms[:100], self.counts[:100])

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


if __name__ == '__main__':
    data = pd.read_csv('data/mwmbl-crawl-terms.csv')
    completer = Completer(data)
    start = datetime.now()
    completer.complete('fa')
    end = datetime.now()
    print("Time", end - start)
