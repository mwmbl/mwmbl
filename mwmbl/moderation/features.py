"""Featurisation for the domain moderation suggester.

One module builds the feature matrix for both training and serving, so the two cannot drift
apart - the classic way a model that scored well offline quietly stops working in production.

What is deliberately *not* here: liveness, redirects and blocklist membership. Those are
handled by mwmbl.moderation.rules as deterministic checks. Keeping them out of the learned
model avoids a training hazard - the crawl that produces evidence happens now, but the labels
were made up to two years ago, so a site that was live when it was approved in 2024 and is
dead today would teach the model "dead -> approve". The model only judges what is stable about
a domain: whether its name and its text read as spam, promotion or a non-English site.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from logging import getLogger

import numpy as np
from scipy.sparse import csr_matrix, hstack
from sklearn.feature_extraction.text import TfidfVectorizer

logger = getLogger(__name__)

# Substrings that recur in the SEO/AI-slop domains moderators reject: aianimegenerator.cloud,
# seobacklinkhub.org, stepupsipcalculator.net, bestaitoolsforthat.com, collegetools.io.
SPAM_WORDS = [
    "ai", "seo", "generator", "calculator", "tool", "free", "best", "top", "online", "shop",
    "buy", "cheap", "review", "guide", "app", "hub", "pro", "download", "crypto",
]

# How much page text the model reads. Titles and extracts are already truncated by the
# crawler (65 and 155 chars), so three pages is a few hundred characters at most.
MAX_TEXT_CHARS = 2000

# Counts are scaled into roughly [0, 1] so the regularisation is comparable across them and
# the TF-IDF blocks. Indicators are already in [0, 1] and are deliberately *not* scaled: a 0/1
# divided by 40 forces its coefficient forty times larger to say the same thing, which the L2
# penalty then charges for, and has_text is the one feature that most needs to be heard.
SHAPE_SCALE = 40.0
COUNT_FEATURE_NAMES = ["len", "labels", "digits", "hyphens", "sld_len", "spamwords", "tld_len"]
INDICATOR_FEATURE_NAMES = ["www"]
# Held out separately from the text block, because the two can fail independently: the block
# can earn its place while the indicator is a trap. See Featuriser.
OPTIONAL_INDICATOR_FEATURE_NAMES = ["has_text"]
NUM_SHAPE_FEATURES = (len(COUNT_FEATURE_NAMES) + len(INDICATOR_FEATURE_NAMES)
                      + len(OPTIONAL_INDICATOR_FEATURE_NAMES))


@dataclass
class ModerationExample:
    """One thing to score: a domain, plus whatever page text we managed to crawl for it."""
    domain: str
    page_texts: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(self.page_texts)[:MAX_TEXT_CHARS]


def tld(domain: str) -> str:
    labels = domain.lower().split(".")
    return labels[-1] if len(labels) > 1 else ""


def _tld_tokens(domain: str) -> list[str]:
    return [tld(domain)]


def shape_features(domain: str) -> list[float]:
    """The scaled counts, in COUNT_FEATURE_NAMES order."""
    host = domain.lower()
    labels = host.split(".")
    return [
        len(host),
        len(labels),
        sum(character.isdigit() for character in host),
        host.count("-"),
        len(labels[0]),
        sum(word in host for word in SPAM_WORDS),
        len(tld(domain)),
    ]


def indicator_features(example: "ModerationExample", use_has_text: bool = True) -> list[float]:
    """The unscaled 0/1 features, in INDICATOR + OPTIONAL_INDICATOR name order."""
    indicators = [float(example.domain.lower().startswith("www."))]
    if use_has_text:
        indicators.append(float(bool(example.text.strip())))
    return indicators


class Featuriser:
    """Fits the vectorisers on a training set, then transforms anything the same way.

    Pickled whole as part of the model artifact, so serving uses the exact vocabulary that
    training saw rather than rebuilding one from different data.
    """

    def __init__(self, use_text: bool = True, use_has_text: bool = True):
        """``use_text`` fits the page-text vocabulary; ``use_has_text`` the "was it crawled at
        all" indicator. Two flags rather than one because the two can fail independently, and
        the August 2026 numbers suggest they did.

        ``has_text`` exists because an empty text block and a page whose words are all out of
        vocabulary produce the same all-zero TF-IDF row, and they are not the same thing. But
        evidence is crawled newest-first, so under a chronological split the indicator is also
        a proxy for *recency* - 46% of training rows against 92% of cold-start test rows in the
        first production run - and recency is when the .ai spam wave happened. A model can
        therefore learn ``has_text -> reject`` from the training mix and then apply it to a test
        set where almost everything has text: the ordering barely moves, so PR-AUC does not
        notice, but the whole score distribution shifts through the serving threshold and
        precision at the head collapses. Recall at 75% precision halved between the two
        production runs, which is the shape of exactly that.

        So it is ablatable, and the honest fix is to even out the coverage by backfilling
        evidence for older submissions rather than to argue about the feature.
        """
        self.use_text = use_text
        self.use_has_text = use_has_text
        # char_wb over the domain: catches the morphology of generated spam names, and the
        # word-boundary variant keeps n-grams from spanning label boundaries.
        self.domain_chars = TfidfVectorizer(
            analyzer="char_wb", ngram_range=(2, 5), min_df=2, sublinear_tf=True)
        # The TLD earns its own block rather than being left to the char n-grams: .ai was 30/33
        # rejected and .org 24/1079, and that is a whole-token effect, not a substring one.
        self.tlds = TfidfVectorizer(
            analyzer="word", tokenizer=_tld_tokens, token_pattern=None, min_df=1)
        self.text = TfidfVectorizer(
            analyzer="word", ngram_range=(1, 2), min_df=2, sublinear_tf=True,
            strip_accents="unicode", lowercase=True)

    def fit_transform(self, examples: list[ModerationExample]) -> csr_matrix:
        domains = [example.domain for example in examples]
        blocks = [
            self.domain_chars.fit_transform(domains),
            self.tlds.fit_transform(domains),
        ]

        # Page text is optional, and often absent: the first training run happens before the
        # evidence backfill has crawled anything, and a domain that could not be fetched has
        # no text at all. min_df=2 then prunes the vocabulary to nothing, which sklearn
        # raises on - so a text block is only added when the text can actually support one.
        texts = [example.text for example in examples]
        try:
            if not self.use_text:
                raise ValueError("text block disabled for this fit")
            blocks.append(self.text.fit_transform(texts))
        except ValueError:
            logger.info("No text block over %d examples (disabled, or not enough text to fit "
                        "one); the model will judge on the domain name alone", len(examples))
            self.text = None

        blocks.append(self._shape_block(examples))
        return hstack(blocks).tocsr()

    def transform(self, examples: list[ModerationExample]) -> csr_matrix:
        domains = [example.domain for example in examples]
        blocks = [
            self.domain_chars.transform(domains),
            self.tlds.transform(domains),
        ]
        if self.text is not None:
            blocks.append(self.text.transform([example.text for example in examples]))
        blocks.append(self._shape_block(examples))
        return hstack(blocks).tocsr()

    def _shape_block(self, examples: list[ModerationExample]) -> csr_matrix:
        counts = np.array([shape_features(example.domain) for example in examples], dtype=float)
        indicators = np.array([indicator_features(example, self.use_has_text)
                               for example in examples], dtype=float)
        return csr_matrix(np.hstack([counts / SHAPE_SCALE, indicators]))

    def feature_names(self) -> np.ndarray:
        """Feature labels in matrix order, for turning coefficients into moderator-facing text."""
        names = [
            self.domain_chars.get_feature_names_out(),
            np.array([f"tld={name}" for name in self.tlds.get_feature_names_out()]),
        ]
        if self.text is not None:
            names.append(np.array([f"text={name}"
                                   for name in self.text.get_feature_names_out()]))
        indicators = INDICATOR_FEATURE_NAMES + (
            OPTIONAL_INDICATOR_FEATURE_NAMES if self.use_has_text else [])
        names.append(np.array(COUNT_FEATURE_NAMES + indicators))
        return np.concatenate(names)
