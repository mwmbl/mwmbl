Contributions are very welcome!

Please join the discussion at https://matrix.to/#/#mwmbl:matrix.org and let us know what you're planning to do.

See https://book.mwmbl.org/page/developers/ for a guide to development.


Automated issue work
--------------------

Two labels hand an issue to the Claude automation in
`.github/workflows/claude-issues.yml`, which runs hourly:

- **ready to plan** — it opens a pull request adding an implementation plan under
  `docs/plans/`. Merging that pull request starts the work, one pull request per section.
- **ready to code** — it opens a single pull request implementing the issue, or writes a
  plan first if the issue is too big for one.

Only one automated pull request is open per issue at a time, so the next part waits for
you to merge the previous one. See `docs/plans/README.md` for the plan format and
`AGENTS.md` for the conventions the automation follows.
