# Polymaker production (EC2, eu-west-1)

One Ubuntu 24.04 `t3.medium` in **eu-west-1** runs two processes:

- **Monitor** (`polymaker-monitor.service`) — always-on `poly-sharp-finder` (whale + smart-wallet convergence).
- **Sharp pipeline** (`polymaker-sharp.timer`) — every 30 minutes, scrape MLB + WNBA splits and trade only when every required source is on **today's Pacific slate**.

## 1. Put secrets in SSM

```bash
REGION=eu-west-1
aws ssm put-parameter --region $REGION --name /polymaker/POLY_PRIVATE_KEY --type SecureString --value '0x...' --overwrite
aws ssm put-parameter --region $REGION --name /polymaker/POLY_FUNDER --type SecureString --value '0x...' --overwrite
aws ssm put-parameter --region $REGION --name /polymaker/CONVEX_HTTP_URL --type SecureString --value 'https://<deployment>.convex.site' --overwrite
aws ssm put-parameter --region $REGION --name /polymaker/CONVEX_PUBLISH_TOKEN --type SecureString --value '<same as Convex PUBLISH_TOKEN>' --overwrite
aws ssm put-parameter --region $REGION --name /polymaker/POLYMAKER_LIVE --type SecureString --value '1' --overwrite
```

`POLYMAKER_LIVE=1` sends real CLOB buys. Leave it unset (or `0`) for dry-run.

Convex: `npx convex env set PUBLISH_TOKEN <token>` in `dashboard/`, then deploy the `trades` table + HTTP routes.

## 2. Build and push the image (optional; from this repo root)

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=eu-west-1
# terraform apply first so the ECR repo exists
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com
docker build -f infra/Dockerfile -t polymaker:latest .
docker tag polymaker:latest $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/polymaker:latest
docker push $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/polymaker:latest
```

## 3. Terraform

```bash
cd infra/terraform
terraform init
terraform apply \
  -var="aws_region=eu-west-1" \
  -var="image_uri=$ACCOUNT.dkr.ecr.eu-west-1.amazonaws.com/polymaker:latest" \
  -var="repo_url=https://github.com/<you>/polymaker.git" \
  -var="git_ref=main"
```

Optional SSH: `-var='key_name=my-key' -var='ssh_cidr=x.x.x.x/32'`.

On boot, user-data pulls `/polymaker/*` into `/etc/polymaker.env`, starts the monitor, and enables the 30-minute timer.

## Logs (CloudWatch)

Container stdout/stderr from the monitor and sharp pipeline go to log group **`/polymaker/trading-bot`** in **eu-west-1**.

- Monitor stream: `monitor`
- Sharp pipeline stream: `sharp`

Console: https://eu-west-1.console.aws.amazon.com/cloudwatch/home?region=eu-west-1#logsV2:log-groups/log-group/$252Fpolymaker$252Ftrading-bot

You do not need a Session Manager shell to read these. Local `docker-compose.local.yml` still uses json-file logs and does not send to CloudWatch.

## 4. First-boot checks (SSM Session Manager)

```bash
aws ssm start-session --target <instance-id> --region eu-west-1
docker exec -it polymaker-monitor uv run polymaker doctor
journalctl -u polymaker-monitor -f
journalctl -u polymaker-sharp -f
systemctl list-timers polymaker-sharp.timer
```

## Local full stack (same two processes as EC2)

You do not need AWS to exercise the monitor + 30-minute pipeline. Keep
`POLYMAKER_LIVE` unset or `0` in `trading-bot/.env` so nothing is sent to the CLOB.

**Native (fastest — uses your venv, no image build):**

```bash
cd trading-bot
uv sync
uv run playwright install chromium   # once; WNBA scrapes need it
# fill .env (wallet keys; Convex optional until you go live)
chmod +x scripts/run_local_stack.sh
# 30-minute loop (same cadence as systemd):
./scripts/run_local_stack.sh
# or a 2-minute loop while you watch logs:
POLYMAKER_SHARP_INTERVAL_SEC=120 ./scripts/run_local_stack.sh
```

That starts:

- `scripts/run_monitor.py` — websocket + trade poller (dry-run would-buys)
- `scripts/run_sharp_loop.py` — scrape / align / trade-sharp / refresh watch list, then sleep

Ctrl-C stops both. Convex (`npx convex dev` in `dashboard/`) is optional in dry-run; the pipeline still publishes a `polymarket_trades` snapshot if `CONVEX_HTTP_URL` + `CONVEX_PUBLISH_TOKEN` are set.

**Docker (closer to the AMI):**

```bash
# from trading-bot/; first build is slow (Playwright Chromium)
touch state.db
echo '[]' > poly-sharp-finder/watch_list.json
docker compose -f infra/docker-compose.local.yml up --build
```

This is the production `monitor` service plus a `sharp-loop` container instead of systemd.timer. Do not use `infra/docker-compose.yml` on a laptop — that file expects `/etc/polymaker.env` on the EC2 host.

## 5. Alignment gate

The pipeline scrapes every 30 minutes even when sources disagree. It **does not trade** a league until every required source is on the same Pacific calendar day with overlapping matchups:

- MLB: PlayerProps, VSiN, SportsBettingDime
- WNBA: DraftKings, VSiN, TheSpread

EVA / Covers are enrichment only and never block trading.
