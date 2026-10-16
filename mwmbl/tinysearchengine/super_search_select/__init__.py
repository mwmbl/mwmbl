"""Source selection for Super Search.

With ~100 registered sources, querying every one per search is too slow and
expensive. This package selects a small subset (~10) to query, using a
contextual bandit whose strongest features are the cosine similarity between the
query and each site's accumulated content profile (bag-of-words and character
n-grams, hashed/random-projected, cached in Redis).

Modules:
- ``vectors``   — feature-hashing projection, stop-words, char n-grams, cosine.
- ``profiles``  — per-site content-profile read/update in Redis.
- ``features``  — build the context feature vector for a (query, site) pair.
- ``rstats``    — per-source reward EMA in Redis (the contribution_ema feature).
- ``xgb_model`` — the contextual-bandit reward model: training, artifact, serving.
- ``policy``    — epsilon-greedy selection over the model's predicted rewards.
- ``registry``  — site metadata (field, popularity, domain) for features/priors.
"""
