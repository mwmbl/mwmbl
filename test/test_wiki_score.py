import json
import random
from pathlib import Path

from mwmbl.tinysearchengine.rank import get_wiki_score

WIKI_STATS_PATH = Path(__file__).parent.parent / "mwmbl" / "resources" / "wiki_stats.json"


def test_wiki_score_matches_the_page_view_counts():
    """The table mwmbl_rank embeds gives the same scores as dividing the counts in
    wiki_stats.json by the largest, which is what rank.py used to do with a dict."""
    counts = json.loads(WIKI_STATS_PATH.read_text())
    max_count = max(counts.values())
    # A title with a slash in it is never matched: the title is the URL's last path segment.
    titles = [title for title in counts if "/" not in title]
    sample = titles[:100] + random.Random(0).sample(titles, 1000) + [t for t in titles if not t.isascii()][:100]

    for title in sample:
        assert get_wiki_score(f"https://en.wikipedia.org/wiki/{title}") == counts[title] / max_count


def test_wiki_score_is_zero_for_unknown_and_empty_titles():
    assert get_wiki_score("https://en.wikipedia.org/wiki/Totally_Unknown_Article_XYZ") == 0.0
    assert get_wiki_score("https://en.wikipedia.org/wiki/") == 0.0
