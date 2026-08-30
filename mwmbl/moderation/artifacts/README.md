# Domain moderation model artifact

`model.joblib` is the trained approve/reject suggester; `metrics.json` is how it scored on
held-out real decisions when it was built. Both are committed deliberately, for the same reason
`super_search_select/artifacts/xgb` is: without an artifact the suggester degrades to the
deterministic checks alone, so shipping one means a fresh deploy is useful on day one instead of
after the first retrain.

**This directory is the warm start, and nothing writes to it.** A retrain publishes a
`ModerationModelArtifact` row in Postgres, and that row is what every worker serves and what the
gate compares against; these files are read only when the table is empty. Writing the artifact
here would mean each deploy reverting the retrain to whatever was last committed, and each
worker replica keeping its own copy.

## Regenerating

    uv run manage.py train_domain_moderation_model --dry-run             # metrics only
    uv run manage.py train_domain_moderation_model --dry-run --ablate    # + feature ablation
    uv run manage.py train_domain_moderation_model                       # publish if it passes

The gate loads the published model, scores it and the candidate on the *same* held-out rows, and
bootstraps the difference; the candidate ships unless it is confidently worse. Raw PR-AUC is never
compared across two runs, because its floor is the positive rate — the August 2026 retrain was
blocked for a 0.11 "regression" that was entirely a shift in prevalence after migration 0037. See
`mwmbl.moderation.train`. Publishing writes a new artifact row, which every worker picks up within
a minute, and schedules `rescore_pending_submissions` to re-score the existing queue.

Every slice also reports `at_thresholds`: precision, recall and the unsure share at
`MODERATION_REJECT_THRESHOLD` and `MODERATION_APPROVE_THRESHOLD`, each with an interval. PR-AUC
summarises every threshold, including ones nothing runs at, so a model can hold its ranking while
its score distribution drifts through the two the server serves — read `reject_precision` and
`approve_error_rate` before believing a retrain is an improvement. `--ablate` reports the shipped
model, the same model plus the `has_text` indicator, and the domain-name-only model on the same
split; `--no-text` and `--has-text` fit those variants directly.

Measured on production data with evidence backfilled to even coverage (94% of training rows, 92%
of cold-start test rows), the page-text vocabulary is worth **+0.230 normalised AP** on the
cold-start slice — 0.622 against 0.392 for domain-name-only — and lifts reject recall from 0.111
to 0.153. It is the largest single lever measured on this model.

The `has_text` indicator is **off**. It was measured twice and lost twice: at skewed coverage
(0.535 against 0.521) and again at even coverage, by more (0.622 against 0.598, reject precision
0.707 against 0.682). Once nearly everything has been crawled it is close to a constant, and the
rows it does fire on — the ones nothing could fetch — are already settled decisively by the
liveness check in `rules.py`. `--ablate` still reports the row, so a crawl that silently degrades
would show up as the indicator regaining value.

To reproduce the numbers without a production database, train from the sanitized judgments export
instead:

    uv run python scripts/moderation_eval.py
    uv run python scripts/moderation_eval.py --write-artifact   # overwrite the files here

**Run `--write-artifact` after any change to the feature set.** A featuriser is pickled whole, so
adding or moving a feature leaves this artifact describing a matrix the code no longer builds. It
then fails the probe prediction that `mwmbl.moderation.model` makes at load time and is dropped,
and every deploy runs on the deterministic checks alone until the first retrain publishes. The
probe is what turns that from an exception per domain inside the enrichment task into an honest
"not assessed yet" — it is not a licence to leave the artifact stale.

## This table is shared between deployments

`api.mwmbl.org` and `beta.mwmbl.org` point at the same Postgres instance, so they share this
table — and the `background_task` queue with it. `_published_stamp()` returns the newest row in
the database, not the newest row *this deployment* published, so whenever the two are running
different code the newest row is routinely the other one's.

That matters because a featuriser is pickled whole. A model trained by code with one more shape
feature than a deployment builds produces a matrix that deployment cannot use, and there is no
way for it to know in advance. In August 2026 a retrain on beta (nine shape features) published
into the shared table while api was still on `main` (eight); api picked it up on its next refresh
and every one of the 2,409 rows in the queue rescore failed with `X has 57673 features, but
LogisticRegression is expecting 57674`, retrying on the same exception. The rescore task is in
the shared queue too, so it does not even reliably run on the deployment that scheduled it.

Three things now contain this, and none of them is a substitute for the others:

- `is_compatible()` probes an artifact at load and refuses one this code cannot featurise.
- `get_model()` keeps serving what it has when the newest row is unusable, rather than dropping
  to a fallback once a minute because a sibling deployment retrained.
- `suggest()` degrades to UNSURE if a model that loaded still fails to score, so one unusable
  artifact cannot fail a whole queue rescore.

The standing fix is to stop sharing: either give the deployments separate databases, or scope
the artifact table per deployment. Until then, **retrain from the deployment whose code is
oldest**, so what it publishes is usable by both. If a bad row lands anyway, delete it — every
deployment falls back to the warm start here.

## What this version was trained on

The committed artifact comes from the judgments export, which carries no page text. Only the 21
hand-written seed rows have any, so the text block exists but is fitted on those alone and
contributes almost nothing: in practice this model judges on the domain name, TLD and shape, and
`has_text` is 0 for every real row it saw. Once `backfill_domain_evidence` has crawled real
submissions, a retrain picks up the titles and extracts of the three crawled pages and the text
block becomes real. Expect the first post-backfill retrain to change the artifact substantially,
and read `train_rows_with_text` against the per-slice `rows_with_text` before believing anything
about whether the text is helping — evidence is crawled newest-first, so a chronological split
puts most of the text on the test side.

## Format

`joblib`, because the model is a fitted scikit-learn `TfidfVectorizer` vocabulary plus two
`LogisticRegression` heads, and ONNX export of a TF-IDF pipeline is far more trouble than it is
worth at this size. Two consequences worth knowing:

- Unpickling executes code, so this file — and the artifact rows, which anything that can write
  to the database could replace — is trusted exactly as much as the rest of the repo. Do not
  point `DOMAIN_MODERATION_MODEL_DIR` at a directory anyone else can write to.
- A scikit-learn upgrade can make an old pickle unloadable. `mwmbl.moderation.model` catches that
  and logs it, falling back to this committed artifact and then to the deterministic checks — so
  the symptom is suggestions reverting to an older model version, or saying UNSURE, and the fix
  is a retrain.
