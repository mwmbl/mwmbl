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

    uv run manage.py train_domain_moderation_model --dry-run   # report metrics only
    uv run manage.py train_domain_moderation_model             # publish if it passes the gate

The gate compares the new model's cold-start PR-AUC against the *lower bound* of the incumbent's
bootstrap interval — the metrics stored beside the published model, falling back to `metrics.json`
until something has been published — so a change that only looks like an improvement does not
ship. Publishing writes a new artifact row, which every worker picks up within a minute, and
schedules `rescore_pending_submissions` to re-score the existing queue with the new model.

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

- Unpickling executes code, so this file — and the artifact rows, which anything that can write
  to the database could replace — is trusted exactly as much as the rest of the repo. Do not
  point `DOMAIN_MODERATION_MODEL_DIR` at a directory anyone else can write to.
- A scikit-learn upgrade can make an old pickle unloadable. `mwmbl.moderation.model` catches that
  and logs it, falling back to this committed artifact and then to the deterministic checks — so
  the symptom is suggestions reverting to an older model version, or saying UNSURE, and the fix
  is a retrain.
