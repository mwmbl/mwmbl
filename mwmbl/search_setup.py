from pathlib import Path

from django.conf import settings

from mwmbl.apps import index_shape_mismatch
from mwmbl.indexer.blacklist_snapshot import get_snapshot_blacklist
from mwmbl.tinysearchengine.completer import Completer
from mwmbl.tinysearchengine.indexer import Document, TinyIndex
from mwmbl.tinysearchengine.jev_rank import JevRanker
from mwmbl.tinysearchengine.ltr import RustXGBPipeline
from mwmbl.tinysearchengine.ltr_rank import CombinedLTRRanker, LTRRanker
from mwmbl.tinysearchengine.mmr_rank import MMRRanker

# Pull the blacklist snapshot in at worker startup rather than letting the first query
# pay for the ~11 MB Redis read. Subsequent refreshes happen on a daemon thread.
get_snapshot_blacklist()

completer = Completer()
index_path = Path(settings.DATA_PATH) / settings.INDEX_NAME
# Nothing creates the index on startup, so a missing file here is most likely a data volume
# that did not mount - which an empty index would have hidden.
if not index_path.exists():
    raise FileNotFoundError(f"No search index at {index_path}: create it with `manage.py create_index`")
tiny_index = TinyIndex(item_factory=Document, index_path=index_path)
# A size that disagrees with settings means NUM_PAGES was changed against the only copy of
# the crawl. Refuse to serve rather than read it with the wrong geometry.
index_mismatch = index_shape_mismatch(tiny_index, settings.NUM_PAGES)
if index_mismatch is not None:
    raise ValueError(f"{index_mismatch} for {index_path}")
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
# Staan's ranking as features, which only CombinedLTRRanker supplies. Jev orders the final
# list from the model's output, without MMR.
combined_ltr_model = RustXGBPipeline.from_model_path(str(settings.COMBINED_MODEL_PATH))
combined_ranker = JevRanker(CombinedLTRRanker(tiny_index, completer, combined_ltr_model))
