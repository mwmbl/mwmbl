# Domain moderation model artifact

`model.joblib` is the trained approve/reject suggester; `metrics.json` is how it scored on
held-out real decisions when it was built. Both are committed deliberately, for the same reason
`super_search_select/artifacts/xgb` is: without an artifact the suggester degrades to the
deterministic checks alone, so shipping one means a fresh deploy is useful on day one instead of
after the first retrain.

## Regenerating

    uv run manage.py train_domain_moderation_model --dry-run   # report metrics only
    uv run manage.py train_domain_moderation_model             # publish if it passes the gate

The gate compares the new model's cold-start PR-AUC against the *lower bound* of the incumbent's
bootstrap interval in `metrics.json`, so a change that only looks like an improvement does not
ship. Publishing also schedules `rescore_pending_submissions`, which re-scores the existing queue
with the new model.

To reproduce the numbers without a production database, train from the sanitized judgments export
instead:

    uv run python scripts/moderation_eval.py

## What this version was trained on

The committed artifact comes from the judgments export, which carries no page text, so its
featuriser has **no text block** — it judges on the domain name, TLD and shape alone. Once
`backfill_domain_evidence` has crawled real submissions, a retrain picks up the titles and
extracts of the three crawled pages and the text block appears. Expect the first post-backfill
retrain to change the artifact substantially.

## Format

`joblib`, because the model is a fitted scikit-learn `TfidfVectorizer` vocabulary plus two
`LogisticRegression` heads, and ONNX export of a TF-IDF pipeline is far more trouble than it is
worth at this size. Two consequences worth knowing:

- Unpickling executes code, so this file is trusted exactly as much as the rest of the repo. Do
  not point `DOMAIN_MODERATION_MODEL_DIR` at a directory anyone else can write to.
- A scikit-learn upgrade can make an old pickle unloadable. `mwmbl.moderation.model` catches that
  and logs it rather than failing, falling back to the deterministic checks — so the symptom is
  "every suggestion says UNSURE", and the fix is a retrain.
