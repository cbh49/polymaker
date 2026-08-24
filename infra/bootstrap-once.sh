#!/bin/bash
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates curl git jq unzip docker.io docker-compose-v2

if ! command -v aws >/dev/null 2>&1; then
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
  unzip -q -o /tmp/awscliv2.zip -d /tmp
  /tmp/aws/install
fi

systemctl enable --now docker

REGION=eu-west-1
REPO_URL=https://github.com/cbh49/polymaker.git
GIT_REF=main
APP_ROOT=/opt/polymaker

mkdir -p "$APP_ROOT" /var/lib/polymaker/journal /var/lib/polymaker/logs /var/lib/polymaker/output \
  /var/lib/polymaker/signals /var/lib/polymaker/intents
echo '[]' > /var/lib/polymaker/watch_list.json
touch /var/lib/polymaker/state.db

{
  echo "# generated from SSM"
  for name in POLY_PRIVATE_KEY POLY_FUNDER CONVEX_HTTP_URL CONVEX_PUBLISH_TOKEN POLYMAKER_LIVE POLYGON_RPC_URL \
      X_API_KEY X_API_KEY_SECRET X_ACCESS_TOKEN X_ACCESS_TOKEN_SECRET X_WHALE_POSTS; do
    val="$(aws ssm get-parameter --name "/polymaker/${name}" --with-decryption --query Parameter.Value --output text --region "$REGION" 2>/dev/null || true)"
    if [ -n "$val" ] && [ "$val" != "CHANGE_ME" ] && [ "$val" != "None" ]; then
      printf '%s=%s\n' "$name" "$val"
    fi
  done
  echo "AWS_REGION=$REGION"
} > /etc/polymaker.env
chmod 600 /etc/polymaker.env

if [ ! -d "$APP_ROOT/.git" ]; then
  git clone --depth 1 --branch "$GIT_REF" "$REPO_URL" "$APP_ROOT"
else
  git -C "$APP_ROOT" fetch --depth 1 origin "$GIT_REF"
  git -C "$APP_ROOT" checkout "$GIT_REF"
  git -C "$APP_ROOT" pull --ff-only origin "$GIT_REF" || true
fi

docker build -f "$APP_ROOT/infra/Dockerfile" -t polymaker:latest "$APP_ROOT"

cp "$APP_ROOT/infra/systemd/"*.service /etc/systemd/system/
cp "$APP_ROOT/infra/systemd/"*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now polymaker-monitor.service
systemctl enable --now polymaker-sharp.timer
systemctl start polymaker-sharp.service || true
echo BOOTSTRAP_OK
systemctl is-active polymaker-monitor || true
