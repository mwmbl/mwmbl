# Privacy policy for social.mwmbl.org (draft)

A draft for the front-end design session. social.mwmbl.org's `privacy_policy_url` points at
`https://mwmbl.org/social/privacy/`, so the front-end must serve this text there before Mwmbl
Social goes live. Fill the **[TO FILL]** placeholders first. Mwmbl's API does not serve it.

---

social.mwmbl.org is run by Mwmbl, the non-profit open source search engine. It runs
[Mwmbl Social](https://mwmblsocial.org), open source software
([source](https://github.com/mwmbl/social), AGPL-3.0) that indexes fediverse posts and accounts
whose authors opted in. This page covers social.mwmbl.org. Your Mwmbl account is covered by
[Mwmbl's data privacy policy](https://mwmbl.org/static/data-privacy/).

**[TO FILL: effective date]**

## What is indexed

- Only fully public posts. Unlisted, quiet public, followers-only and direct posts never are.
- An account is indexed only if it has *discoverable* turned on, and its posts only if it also
  has *indexable* turned on. In Mastodon these are under Preferences > Public profile > Privacy
  and reach.
- Lemmy accounts don't have these settings, so a Lemmy author who hasn't set them counts as
  agreeing. Where one is explicitly off, that is respected.
- Following `@mwmbl-social@social.mwmbl.org` opts you in for any of these settings your account
  doesn't have. A setting that is explicitly off still wins, and unfollowing withdraws the opt-in.

## Where it comes from

- Servers whose admins connected them and turned on data sharing tell us about posts and accounts.
- Lemmy communities, followed only when their instance's admins agreed, and accounts that follow us.
- Lemmy post and comment addresses found by Mwmbl's crawler. These are fetched only where the
  instance's robots.txt allows it.

Whatever the source, we fetch each post or account from its own server and check the rules above
before indexing it.

## What we store

- For a post: its address, its author's address, its title and language if it has them, when it
  was published and last checked, and an extract of up to about 300 characters of its text,
  including any content warning and hashtags.
- For an account: its address and handle, display name, a shortened bio, and the languages it
  posts in.
- What is needed to run the service: the servers connected to it and their settings, a queue of
  addresses to fetch, what we know about each host (its software and robots.txt), removals still
  being applied, the accounts that follow us and when, the accounts and communities we follow,
  and removal requests.

We never store non-public posts anywhere, not even in logs. Mwmbl Social does not keep the
searches connected servers send it.

**[TO FILL: access logs of the web server in front of social.mwmbl.org, and how long they are
kept]** (they would record the requesting server's IP address and the request URL, which for an
account search includes the search term).

## Who sees it

Indexed posts and accounts are public already. They are shown to connected servers in answer to
account searches and follow recommendations, through Mwmbl Social's search API, and, once that is
built, in Mwmbl search. Mwmbl will not copy them into its own web index, so removals here apply
everywhere.

## How things are removed

- Deleting a post, making it non-public, or turning off *discoverable* or *indexable* removes it.
  When a connected server tells us, that happens straight away. Everything indexed is also
  checked again at least once a week, which catches the rest.
- Unfollowing `@mwmbl-social@social.mwmbl.org` removes posts indexed because you followed it.
- To have a post, an account or a whole domain removed for good, email
  [info@mwmbl.org](mailto:info@mwmbl.org) with its address. It will not be indexed again.
- A server admin can disconnect their server on **[the connect page,
  https://mwmbl.org/social/connect/]**, and should also remove Mwmbl Social from their server's
  admin panel.

## If you connect a server

Mwmbl records, with your Mwmbl account, the server address you entered, its registration with
Mwmbl Social, the approval link and fingerprint, and when you connected and disconnected it.
Other Mwmbl users can't see these; Mwmbl's operators can, as they can the rest of your account.
Mwmbl Social itself does not know which Mwmbl account registered a server.

## Contact

Questions about this policy: [info@mwmbl.org](mailto:info@mwmbl.org).
