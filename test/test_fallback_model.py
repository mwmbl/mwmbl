"""Trigger logic for the Super-Search-as-fallback ranking model (no network)."""
from mwmbl.rankeval.evaluation.evaluate import RankingModel


class _FixedModel(RankingModel):
    def __init__(self, urls, calls=None):
        self.urls = urls
        self.calls = calls  # optional list to record invocations

    def predict(self, query):
        if self.calls is not None:
            self.calls.append(query)
        return list(self.urls)


def _fallback(primary_urls, fallback_urls, threshold, fb_calls=None):
    # Imported lazily so the module's django.setup()/search_setup import cost is only
    # paid when these tests actually run.
    from mwmbl.rankeval.evaluation.evaluate_fallback import FallbackRankingModel
    return FallbackRankingModel(
        _FixedModel(primary_urls),
        _FixedModel(fallback_urls, calls=fb_calls),
        threshold=threshold,
    )


def test_serves_primary_when_above_threshold():
    primary = ["a", "b", "c", "d"]
    model = _fallback(primary, ["x", "y"], threshold=3)
    assert model.predict("q") == primary


def test_falls_back_when_at_or_below_threshold():
    model = _fallback(["a", "b", "c"], ["x", "y"], threshold=3)
    assert model.predict("q") == ["x", "y"]


def test_falls_back_on_empty_primary():
    model = _fallback([], ["x", "y"], threshold=3)
    assert model.predict("q") == ["x", "y"]


def test_fallback_not_invoked_when_primary_sufficient():
    fb_calls: list[str] = []
    model = _fallback(["a", "b", "c", "d"], ["x"], threshold=3, fb_calls=fb_calls)
    model.predict("q")
    assert fb_calls == []  # the expensive Super Search pipeline is never run


def test_threshold_zero_only_fires_on_empty():
    assert _fallback(["a"], ["x"], threshold=0).predict("q") == ["a"]
    assert _fallback([], ["x"], threshold=0).predict("q") == ["x"]
