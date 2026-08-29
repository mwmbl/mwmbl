# Domain moderation: design vs. implementation

Design: <https://claude.ai/design/p/bca5ef6e-47c5-44e1-a45b-9e91d3483c53?file=Domain+Moderation+Alternatives.dc.html>

Turn **2a** ("Focus slot") is the built-out target. Turns 1a/1b/1c are the earlier alternatives it
was built from; 1a's status filter is the only thing from them still wanted (gap 8).

All nine gaps below are closed. What follows is what each one turned into, so the reasoning
behind the shape of the API stays attached to the design it came from.

## What the API returns now

`GET /domain-submissions/queue` is one row per **domain**, carrying everything one focus card and
its UP NEXT rows draw — so moving to the next card costs no request:

```
name, submission_count, first_submitted_on, first_submitted_by(+_username),
upvotes, downvotes, https, evidence_state, pages[], suggestion
```

`count` is distinct pending domains — the header's "24 pending". Default ordering is
`submissions` (`-submission_count, -upvotes, submitted_on`), which is the sort the screen labels;
`needs_review`, `confidence` and `oldest` are still accepted.

Decisions, undo and history are all addressed to a domain:

- `POST /domain-submissions/decisions` — `{"decisions": [{"domain": …, "status": …}]}`; each entry
  settles every submission of that name.
- `POST /domain-submissions/domains/{domain}/undo` — back to PENDING, rejection fields cleared.
- `GET /domain-submissions/moderated` — past moderations, one row per domain, filterable by
  `status` / `moderator` / `name`.

## Gap by gap

### 1. The queue is per domain

`one_row_per_domain` (`mwmbl/moderation/suggest.py`) keeps the **earliest** submission of each
name and annotates `submission_count` onto it.

Deliberately not a `GROUP BY`. The suggestion depends on the submitter's own track record, and a
domain submitted by two accounts has no single one to read — grouping would have meant rewriting
the withheld-approval rule and breaking the `suggestion_for` / `annotate_queue` parity that
`test_queue_display_matches_suggestion_for` holds together. Collapsing to a representative row
instead leaves both of them working on a submission. The earliest row is also the one the card
draws ("first submitted 6 days ago by anon_4417"), so the row we deduplicate to and the row we
show are the same row.

### 2. `submissions` ordering

`QUEUE_ORDERINGS["submissions"]` = `-submission_count, -upvotes, submitted_on`, and it is the
default. How many people asked for a domain is the one signal in the sort that is not the tool's
own opinion, which is why it leads rather than the suggestion's confidence.

### 3. Vote counts aggregated to the domain

`SearchResultVote` gained an indexed `domain` column, derived on `save()` by `utils.bare_host`
(normalise, drop a leading `www.`), with a batched backfill in migration 0036. `annotate_votes`
counts it in SQL against the submission name reduced the same way, so a vote on
`www.example.com/x` counts towards a submission of `example.com`. Coalesced to zero, because a
domain with no votes has to sort with the rest rather than scatter through it as NULL.

A rollup table was the alternative and was rejected: the live aggregate is one indexed subquery
and never stale.

### 4. Sample pages on queue rows

`pages` on `QueueItemSchema`, read off the `DomainEvidence` rows `_queue_items` already fetches
for the suggestion. No extra query.

### 5. Usernames

`first_submitted_by_username` on the queue row and `submitted_by_username` on the detail, both
via `select_related("submitted_by")` — a join, not a query. The numeric ids stay, for filtering.

### 6. The https padlock

`crawl_domain` now retries `http://` once when the https fetch **errors** (not when it answers
with a non-2xx), and records `signals["https"]`. A 404 over https is an answer; asking the same
server the same question without TLS gets the same one.

This changes suggestions: a site serving only over plain HTTP used to be `unreachable` — a
decisive REJECT at 0.9 — and is now reachable with a neutral `no_tls` evidence line. Neutral for
the same reason `robots` is: plenty of the small personal sites this index exists for have no
certificate, and no rejection detail a moderator has written mentions TLS.

`https` is `Optional[bool]`: `null` until the domain is crawled, because an uncrawled domain must
not draw an open padlock. Existing evidence rows report `null` until they go stale
(`MODERATION_EVIDENCE_MAX_AGE_DAYS`) or `backfill_domain_evidence` is run.

### 7. Undo

`POST /domain-submissions/domains/{domain}/undo` restores PENDING and clears
`rejection_reason` / `rejection_detail`, and **leaves `suggested_status`, `suggested_reason`,
`suggestion_confidence` and `suggestion_model_version` alone** — the record of what was on screen
when the decision being undone was made, which is the one thing those columns exist for and
exactly what re-posting a PENDING status destroyed.

`status_changed_by` / `status_changed_on` do move to the undoer: the field means "who last
changed the status", and that is now them.

`.update()` fires no `post_save`, so undoing an *approval* schedules a blacklist snapshot rebuild
explicitly, with the same debounce as `mwmbl/signals.py`.

The "Reviewed this session" tray is client-side session state.

### 8. Viewing and changing past moderations

`GET /domain-submissions/moderated`, moderator-only, paginated, one row per domain represented by
its **most recently touched** submission — a history row shows the decision that currently
stands. Rows carry the `suggested_*` audit columns, so a moderator revisiting a call sees what
was on screen then rather than what the model would say today.

Changing a past decision is a normal POST to `/decisions`: a decision applies to every submission
of the name, decided or not, so re-deciding needs no separate endpoint and a domain can never sit
half approved and half rejected.

The trade-off: re-deciding overwrites `status_changed_*` and the `suggested_*` columns on the
already-decided rows of that name. An append-only `ModerationDecision` log would keep both, and
is the change to make if decision history is ever read.

### 9. Rejection reasons are validated

`RejectionFieldsMixin` in `mwmbl/platform/schemas.py`, shared by `DomainDecision` and
`UpdateDomainSubmission` so the two cannot drift:

- `rejection_reason` must be one of the four choices — Django does not enforce `choices` on
  `save()`, so any string of ≤20 characters used to land in the column;
- `rejection_detail` is required when the reason is `OTHER`;
- `rejection_reason` is only accepted alongside `status=REJECTED`.

A schema validator rather than a view check, so these are 422s naming the field.

**No notification is sent.** The design's "The submitter is told the reason" line is being removed
from the dialog instead; the reason is already visible on the submitter's own submission list.

## Dropped

- **A one-line "why" for the suggestion** (`suggested by the index — 9 submissions from 2
  accounts, heavy downvotes, 4 crawled pages`). The API returns per-check `EvidenceItem` labels
  instead, which a client can join. "from 2 accounts" — the count of distinct submitting accounts
  — is still not computed anywhere; it would be one more correlated subquery if it comes back.

## Known loose end

`views.py`'s `DomainSubmissionApprovalForm` decides submissions without going through
`apply_decision`: it sets neither `status_changed_by` nor the `suggested_*` audit columns. Not one
of these nine gaps, but a second decision path that quietly writes worse records than the API's.
