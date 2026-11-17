# social.mwmbl.org

Deployment config for Mwmbl's hosted instance of [Mwmbl Social](https://github.com/mwmbl/social),
a fediverse search engine that fediverse servers connect to as a FASP. It runs on Dokku on the
same server as Mwmbl, as the app `social.mwmbl.org`, from the image Mwmbl Social publishes
(`ghcr.io/mwmbl/social`, tagged `main` and `sha-<commit>`).

- `config.env`: the non-secret config, as `MWMBL_SOCIAL_*` variables. The fields are described
  in the Mwmbl Social README.
- `secrets.enc.env`: `MWMBL_SOCIAL_ADMIN_TOKENS` and `MWMBL_SOCIAL_CRAWL_TOKENS`, encrypted
  with [SOPS](https://github.com/getsops/sops) to the age keys in the repository's `.sops.yaml`.
- `setup.sh`: creates and configures the Dokku app, and applies both files. Safe to rerun.
- `.github/workflows/deploy-social.yml`: deploys an image tag, run by hand.

Mwmbl Social is a single process that keeps SQLite, its index files and its keys in `/data`,
a storage mount. There must only ever be one container: `setup.sh` scales `web` to 1 and
disables zero-downtime checks, so Dokku stops the old container before starting the new one
(a deploy has a few seconds of downtime). If two did overlap, the second would refuse to start,
because `serve` takes an exclusive lock on `/data/writer.lock`. Don't `ps:scale` it up.

## First-time setup

You need SSH access as `dokku@mwmbl.org`, root on the server for one step, and `sops` and `age`
locally.

1. Point DNS for `social.mwmbl.org` at the server.
2. Fill in `MWMBL_SOCIAL_CONTACT_EMAIL` in `config.env` and check the index sizes (below).
3. Create the secrets file (see [Secrets](#secrets)).
4. Run `deploy/social/setup.sh` from the repository root.
5. As root on the server, give the storage directory to the image's user. Check its uid first:

   ```bash
   docker run --rm --entrypoint id ghcr.io/mwmbl/social:main
   chown <uid>:<gid> /var/lib/dokku/data/storage/social.mwmbl.org
   ```

6. Deploy (below).
7. Run `setup.sh` again: now the app is deployed, it enables Let's Encrypt. This uses the
   letsencrypt email set for the server with `letsencrypt:set --global email`; if there isn't
   one, set it first.
8. Check `https://social.mwmbl.org/actor` answers, and that `https://social.mwmbl.org/`
   redirects to the sign-up page.

`setup.sh` saves config without restarting the app, so after changing `config.env` or the
secrets, run it and then `ssh dokku@mwmbl.org ps:restart social.mwmbl.org`.

### Index sizes

`config.env` sets 1,048,576 pages (4 GiB) for posts and 262,144 (1 GiB) for accounts, 16 times
the self-host defaults. That is a guess at what fits the server's disk, not a measured need:
choose before the first deploy. Both files are allocated in full when created, and an existing
index keeps its size whatever the config says, so resizing later means rebuilding the index.
When an index page is full, its lowest-scored items are dropped.

### Commands not checked against our server

`setup.sh` follows the current Dokku documentation; it has not been run against our server.
Check these on its Dokku version: `storage:ensure-directory --chown false` (deprecated in recent
Dokku in favour of `storage:create`, still accepted), `ports:set` and `ports:report` (Dokku 0.31
or later; older versions use `proxy:ports-set`), `ps:scale --skip-deploy`,
`ps:report --deployed`, and `letsencrypt:active` from the letsencrypt plugin.

## Deploying

Run the "Deploy Mwmbl Social" workflow in GitHub Actions with the image tag to deploy: `main`
for the latest build, or `sha-<commit>` for a particular one, which is also how to roll back.
The workflow resolves the tag to its digest and runs `dokku git:from-image` with it over SSH.
From a machine with access, the same is:

```bash
ssh dokku@mwmbl.org git:from-image social.mwmbl.org ghcr.io/mwmbl/social@sha256:<digest>
```

The image must stay public; a private one would need `dokku registry:login` on the server.

## Secrets

`secrets.enc.env` holds the bearer tokens for the admin API (used by Mwmbl's sign-up page) and
for crawl submissions (used by Mwmbl's crawl pipeline), each at least 16 characters and
comma-separated if there are several. They also need to go wherever Mwmbl calls those APIs from.

One-time key setup:

```bash
age-keygen -o ~/.config/sops/age/keys.txt   # prints your public key, age1...
```

Put every operator's public key in the repository's `.sops.yaml` in place of the placeholder
(comma-separated), then create the file. `sops edit` opens it decrypted in your editor and encrypts it on save:

```bash
sops edit deploy/social/secrets.enc.env
```

```
MWMBL_SOCIAL_ADMIN_TOKENS=<openssl rand -hex 32>
MWMBL_SOCIAL_CRAWL_TOKENS=<openssl rand -hex 32>
```

Edit it the same way later (older sops: `sops deploy/social/secrets.enc.env`). After adding an
operator to `.sops.yaml`, run `sops updatekeys deploy/social/secrets.enc.env`.

`setup.sh` sends the values to Dokku base64-encoded as SSH command arguments, so they briefly
appear in the process list on both machines. Base64 is not encryption.

## Backups

Everything is in `/var/lib/dokku/data/storage/social.mwmbl.org` on the host. Most important is
`keys/`: each registration has its own key there, and losing them breaks every registration,
so every connected server would have to register again. It changes whenever a server
registers, so back it up daily and off the server. Nothing does this yet. A simple approach is
a root cron job on the host:

```bash
cd /var/lib/dokku/data/storage/social.mwmbl.org
sqlite3 mwmbl-social.sqlite ".backup /var/backups/mwmbl-social/sqlite-$(date +%F)"
tar -czf "/var/backups/mwmbl-social/keys-$(date +%F).tar.gz" keys
```

then copying `/var/backups/mwmbl-social` off the server. `.backup` gives a consistent copy of
the database while the app runs. The index files (`*.tinyindex`) can't be copied consistently
while it writes to them; to include them, stop the app (`ps:stop`), copy the whole directory,
and start it again (`ps:start`).

## Metrics

`MWMBL_SOCIAL_METRICS` is off: `/metrics` would be public through Dokku's nginx. Turn it on only
once it is blocked there or scraped privately.
