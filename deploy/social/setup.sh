#!/usr/bin/env bash
# Set up the social.mwmbl.org Dokku app and apply its config. Safe to run again: each step
# checks or overwrites rather than adding. Run it from an operator's machine with SSH access
# to dokku@mwmbl.org and the age key for secrets.enc.env. See README.md in this directory.
set -euo pipefail

APP=social.mwmbl.org
DOKKU_HOST=${DOKKU_HOST:-mwmbl.org}
STORAGE=/var/lib/dokku/data/storage/$APP
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

dokku() {
	ssh -n "dokku@$DOKKU_HOST" "$@"
}

# Dokku splits the SSH command on whitespace and ignores quotes, so every value is sent
# base64-encoded (`config:set --encoded`). Reads KEY=value lines, prints KEY=<base64> lines.
encode_config() {
	local line
	while IFS= read -r line || [[ -n $line ]]; do
		[[ -z $line || $line == \#* ]] && continue
		printf '%s=%s\n' "${line%%=*}" "$(printf '%s' "${line#*=}" | base64 | tr -d '\n')"
	done
}

if grep -q '^[^#]*CHANGE-ME' "$HERE/config.env"; then
	echo "Replace the CHANGE-ME placeholders in $HERE/config.env first." >&2
	exit 1
fi
secrets=$(sops -d "$HERE/secrets.enc.env")

if ! dokku apps:exists "$APP" >/dev/null 2>&1; then
	dokku apps:create "$APP"
fi

# The container runs as the image's `mwmbl` user, not one of the --chown presets, so the
# directory's owner is set by hand once (README.md, first-time setup).
dokku storage:ensure-directory --chown false "$APP"
if ! dokku storage:list "$APP" | grep -qF "$STORAGE:/data"; then
	dokku storage:mount "$APP" "$STORAGE:/data"
fi

dokku domains:set "$APP" "$APP"
# The image EXPOSEs 8080, which Dokku would otherwise publish as port 8080 on the host.
# Only set it once: letsencrypt adds https:443:8080 later, and ports:set would drop it.
if ! dokku ports:report "$APP" --ports-map | grep -qw 'http:80:8080'; then
	dokku ports:set "$APP" http:80:8080
fi

# One process owns the data dir (it holds writer.lock), so never run two containers:
# disabled checks make Dokku stop the old container before starting the new one.
dokku checks:disable "$APP" web
dokku ps:scale --skip-deploy "$APP" web=1

config=()
while IFS= read -r pair; do
	config+=("$pair")
done < <(encode_config <"$HERE/config.env"; encode_config <<<"$secrets")
dokku config:set --encoded --no-restart "$APP" "${config[@]}"

if [[ $(dokku ps:report "$APP" --deployed) != true ]]; then
	echo "Not deployed yet. Deploy it (README.md), then run this again to enable HTTPS."
	exit 0
fi
if [[ $(dokku letsencrypt:active "$APP") != true ]]; then
	dokku letsencrypt:enable "$APP"
fi
echo "Config is saved but not applied to the running app: ssh dokku@$DOKKU_HOST ps:restart $APP"
