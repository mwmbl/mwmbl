# Lint and type-check backlog

Written 2026-09-07, alongside the branch that introduced ruff and ty (PR #369).

That PR turned on a deliberately narrow rule set so the gate could be green from day
one. This note records what was left switched off, what it would cost to switch on, and
what it would buy. Counts are as of PR #369 and will drift.

Everything below is ordered by **bugs prevented per hour of work**, not by count.

## How the checks work today

```
make check          # format-check + lint + typecheck. Exactly what CI and the hook run.
make fix            # ruff format + ruff check --fix
make typecheck-all  # the full ty warning backlog, never fails
```

Ruff is configured in `pyproject.toml` with `select = ["DTZ", "E", "F", "I"]` and `E501`
ignored globally — the formatter owns line length, so `E501` only ever fires on things
it cannot split (long string literals, URLs, the markdown tables in the Super Search API
docs). `DTZ` was added by issue #371, which fixed all 65 naive datetime sites.

ty gates on **error-level diagnostics only**. Every rule with a pre-existing baseline is
downgraded to `warn` in `[tool.ty.rules]`, annotated with its count. The gate is green
today and a *new* category of type error fails the build.

To count any rule before enabling it:

```
uv run ruff check --no-cache --select S113 --statistics
uv run ruff check --no-cache --select S113 --output-format concise   # the actual sites
```

To promote a ty rule once its count reaches zero, change `"warn"` to `"error"` in
`[tool.ty.rules]`. PR #369 did this for `missing-argument` and
`too-many-positional-arguments` after fixing their three violations.

---

## Tier 1 — do these. ~half a day total, highest yield

These target failure modes this codebase has actually hit.

### `S113` — 13 sites — 1h

`requests`/`httpx` calls with no timeout. A hung external host blocks the calling worker
indefinitely. Directly relevant to the Super Search source adapters, which fan out to
half a dozen third-party APIs on every query.

Mechanical: add a timeout. The only judgement is picking the value, and
`settings.SUPER_SEARCH_PER_SOURCE_TIMEOUT` is the obvious precedent for source calls.

### `SIM115` — 19 sites — 1–2h

`open()` without a context manager: leaked file handles. Concentrated in the indexer and
the batch readers, which run in long-lived processes where leaks accumulate. Several are
in `analyse/`, where it matters less.

### `B904` — 12 sites — 30m

`raise` inside `except` without `from`, which discards the original traceback. Costs
nothing to fix and makes production incidents materially easier to read.

---

## Tier 2 — worth doing. ~6–8h

### `B905` — 43 sites

`zip()` without `strict=`. Silently truncates to the shorter iterable, which is a real
correctness trap in the feature-extraction and scoring code where two sequences are
assumed parallel. Each site needs a judgement about whether unequal lengths are possible
and what should happen if they are — so this is not a bulk autofix, despite ruff
offering one under `--unsafe-fixes`.

### `BLE001` — 17 sites

Blind `except Exception`. Some are legitimate (source adapters that must not let one
failing API kill a query — those already have `# noqa: BLE001`), so expect to add
suppressions as often as fixes. Enable it for the value of *forcing the decision* at
each site.

### `RUF013` (9), `B007` (10), `RUF005` (11), `SIM117` (19)

Implicit `Optional`, unused loop variables, list-concatenation in a loop, nested `with`.
Small and mostly mechanical. Low bug yield individually but cheap to clear in one pass,
and they keep the diff noise down when the higher-value rules go on.

---

## Tier 3 — large, low bug yield. Defer or skip

### `UP` — 214 sites

Cosmetic modernisation: `Optional[X]` → `X | None`, `typing.List` → `list`, and so on.
205 of the 214 are autofixable, so this is an afternoon — but it catches no bugs and
produces a 200-file diff that will conflict with anything in flight. If you do it, do it
alone, in its own commit, and add it to `.git-blame-ignore-revs`.

### `TRY003` — 97 sites

Long messages passed to exception constructors. A style opinion, not a bug class.

### `RUF012` — 117 sites, or 36 excluding migrations

Mutable class defaults. About 70% are Django migration `dependencies = [...]` and
`operations = [...]`, which are false positives. Only worth considering with
`**/migrations` excluded, and even then the remaining 36 are mostly Django `Meta` and
DRF-style class attributes where the rule is wrong about the risk.

### `S101` — 1515 sites — **never enable**

`assert` used outside a type check. This is `assert` in the test suite. Enabling it would
be actively harmful.

---

## The ty backlog

741 warnings. The headline number badly overstates the available signal:

| Slice | Count | Actionable? |
|---|---|---|
| Django-model-shaped (`.objects`, `DoesNotExist`, `<fk>_id`, `pk`) | 344 | **No — blocked upstream** |
| In `test/` | 338 | Low value; overlaps the above |
| Production code, not Django-shaped | 269 | Yes, this is the real backlog |
| └ of which `invalid-argument-type` in `mwmbl/` | 87 | The densest real signal |

### The Django 344 are not your problem

ty has no django-stubs plugin support, so `Model.objects` is simply invisible to it.
Installing `django-stubs` does **not** help — this was tested during PR #369 and the
diagnostic count did not move, because django-stubs supplies `objects` through the mypy
plugin, not the stubs themselves.

**Do not budget time for these and do not add suppressions for them.** They will clear
themselves when ty ships Django support. `unresolved-attribute` should stay at `warn`
until then.

### Where the real work is

`invalid-argument-type`, 87 sites in `mwmbl/`. Sampling suggests a mix of genuinely
loose annotations and numpy/pandas dynamism that ty models more strictly than the code
assumes. Realistically 2–3 days.

Worth doing, because this is the same category that produced the three bugs PR #369
fixed: wrong types crossing a module boundary on a path no test exercises. Two of those
three were `TypeError` on the first call, sitting in the tree undetected.

Suggested approach: work directory by directory (`mwmbl/platform` has 85 diagnostics
total, `mwmbl/moderation` 47), and once a directory is clean, keep it clean — ty has no
per-directory severity, so the practical mechanism is to fix a rule everywhere in
production code and then promote it to `error` globally, accepting that `test/` may need
a handful of suppressions.

---

## Suggested order

1. `S113` — an hour, targets a failure mode this codebase has actually hit.
2. `SIM115` and `B904` — another half day, cheap.
3. Tier 2 in one pass.
4. ty `invalid-argument-type` in `mwmbl/` only, by directory, promoting to `error` when
   production code is clean.
5. Leave Tier 3 and the Django 344 alone.
