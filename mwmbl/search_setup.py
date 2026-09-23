from pathlib import Path

from django.conf import settings

from mwmbl.indexer.blacklist_snapshot import get_snapshot_blacklist
from mwmbl.tinysearchengine.completer import Completer
from mwmbl.tinysearchengine.indexer import Document, TinyIndex
from mwmbl.tinysearchengine.ltr import RustXGBPipeline
from mwmbl.tinysearchengine.ltr_rank import CombinedLTRRanker, LTRRanker
from mwmbl.tinysearchengine.mmr_rank import MMRRanker

# Pull the blacklist snapshot in at worker startup rather than letting the first query
# pay for the ~11 MB Redis read. Subsequent refreshes happen on a daemon thread.
get_snapshot_blacklist()

completer = Completer()
index_path = Path(settings.DATA_PATH) / settings.INDEX_NAME
tiny_index = TinyIndex(item_factory=Document, index_path=index_path)
tiny_index.__enter__()

ltr_model = RustXGBPipeline.from_model_path(str(settings.RUST_MODEL_PATH))
# Diversity is applied by the wrapping MMRRanker, which demotes (rather than drops)
# same-domain / near-duplicate results. Unwrap to disable diversity.
# num_wiki_results is left at its default on purpose - see NUM_WIKI_RESULTS. Serving
# passed 3 here while mwmbl.rankeval.ltr.dataset trained on 5, so the model was ranking a
# pool a size it had never been fitted to.
ranker = MMRRanker(LTRRanker(tiny_index, completer, ltr_model, include_wiki=True))

# Combined Search gets its own model and its own ranker, so retraining on the pooled
# Mwmbl + Staan candidate set never moves standard search's ranking. The model uses
# Staan's ranking as features, which only CombinedLTRRanker supplies.
combined_ltr_model = RustXGBPipeline.from_model_path(str(settings.COMBINED_MODEL_PATH))
combined_ranker = MMRRanker(CombinedLTRRanker(tiny_index, completer, combined_ltr_model))
