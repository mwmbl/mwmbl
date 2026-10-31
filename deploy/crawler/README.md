# Automatic crawler updates

The crawler image `mwmbl/crawler:stable` is republished on every crawler release (tag
`crawler-vX.Y.Z`, see `.github/workflows/crawler-release.yml`). These systemd units keep a
Docker Compose crawler (the `docker-compose.yml` in the repository root) on the latest stable
release without anyone having to run `docker compose pull && docker compose up -d` by hand.

The server can also refuse an outdated crawler (mwmbl/mwmbl#573): `POST /api/v1/crawler/results`
answers **426 Upgrade Required** when the crawler's version is below `MIN_CRAWLER_VERSION`,
and the crawler then waits for this timer to replace it.

## Install

```bash
sudo cp deploy/crawler/mwmbl-crawler-updater.service deploy/crawler/mwmbl-crawler-updater.timer /etc/systemd/system/
# Point MWMBL_REPO_DIR at the clone of this repository:
echo 'MWMBL_REPO_DIR=/home/crawler/mwmbl' | sudo tee /etc/default/mwmbl-crawler-updater
sudo systemctl daemon-reload
sudo systemctl enable --now mwmbl-crawler-updater.timer
```

Check on it with `systemctl list-timers mwmbl-crawler-updater.timer` and
`journalctl -u mwmbl-crawler-updater.service`.
