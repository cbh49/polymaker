# Polymaker production (EC2, eu-west-1)

One Ubuntu 24.04 `t3.medium` in **eu-west-1** runs three processes:

- **Monitor** (`polymaker-monitor.service`) — always-on `poly-sharp-finder` (whale + smart-wallet convergence).
- **Sharp pipeline** (`polymaker-sharp.timer`) — every 30 minutes at `:00/:30`, scrape MLB + WNBA + NCAAF + NFL splits and trade only when every required source is on **today's Pacific slate** (NCAAF uses a 6-day weekend window; NFL uses 7 days so Monday night is included).
- **NFL EV pipeline** (`polymaker-ev.timer`) — every 30 minutes at `:15/:45`, scrape RotoWire + Kalshi + Polymarket, buy `tradable` rows with fee-adjusted edge ≥ 5 points (confidence ≥ 0.80, $10 each), and post sportsbook quotes that are ≥ 5 points cheap vs consensus to Discord + X. Live orders require `POLYMAKER_LIVE=1` and a Convex claim so the same contract is not bought twice.

## 1. Put secrets in SSM

```bash
REGION=eu-west-1
aws ssm put-parameter --region $REGION --name /polymaker/POLY_PRIVATE_KEY --type SecureString --value '0x...' --overwrite
aws ssm put-parameter --region $REGION --name /polymaker/POLY_FUNDER --type SecureString --value '0x...' --overwrite
aws ssm put-parameter --region $REGION --name /polymaker/CONVEX_HTTP_URL --type SecureString --value 'https://<deployment>.convex.site' --overwrite
aws ssm put-parameter --region $REGION --name /polymaker/CONVEX_PUBLISH_TOKEN --type SecureString --value '<same as Convex PUBLISH_TOKEN>' --overwrite
aws ssm put-parameter --region $REGION --name /polymaker/POLYMAKER_LIVE --type SecureString --value '1' --overwrite
aws ssm put-parameter --region $REGION --name /polymaker/X_API_KEY --type SecureString --value '...' --overwrite
aws ssm put-parameter --region $REGION --name /polymaker/X_API_KEY_SECRET --type SecureString --value '...' --overwrite
aws ssm put-parameter --region $REGION --name /polymaker/X_ACCESS_TOKEN --type SecureString --value '...' --overwrite
aws ssm put-parameter --region $REGION --name /polymaker/X_ACCESS_TOKEN_SECRET --type SecureString --value '...' --overwrite
aws ssm put-parameter --region $REGION --name /polymaker/X_WHALE_POSTS --type SecureString --value '1' --overwrite
aws ssm put-parameter --region $REGION --name /polymaker/DISCORD_SHARP_WEBHOOK_URL --type SecureString --value 'https://discord.com/api/webhooks/...' --overwrite
aws ssm put-parameter --region $REGION --name /polymaker/DISCORD_EV_WEBHOOK_URL --type SecureString --value 'https://discord.com/api/webhooks/...' --overwrite
aws ssm put-parameter --region $REGION --name /polymaker/X_EV_POSTS --type SecureString --value '1' --overwrite
```

`POLYMAKER_LIVE=1` sends real CLOB buys. Leave it unset (or `0`) for dry-run.

`X_WHALE_POSTS=1` tweets whale detections from the monitor (same X app as MLB POTD). Omit it (or `0`) to keep the monitor silent on X. Keys in `trading-bot/.env` only reach local compose — production reads `/etc/polymaker.env` from these SSM parameters. The same switch (or `X_SHARP_POSTS=1`) also lets the sharp container tweet **at most two** A/A+ sharp-money plays per Pacific day; Discord still gets every A/A+ card. Set `X_SHARP_POSTS=0` to tweet whales only.

`DISCORD_SHARP_WEBHOOK_URL` posts Tier A / A+ sharp-money cards from the 30-minute `sharp` container (`scripts/run_sharp_pipeline.py`). The sent-play cache lives on the host volume `/var/lib/polymaker/output/.discord_sent.json` so reruns do not spam. Omit the parameter to skip Discord.

`DISCORD_EV_WEBHOOK_URL` posts sportsbook +EV cards (fee-free edge ≥ 5 points vs consensus) from the NFL EV container, with a PNG attached. `X_EV_POSTS` tweets the same graphic using the existing X keys; unset posts whenever those keys are present, `0` disables tweets. Dedup cache: `/var/lib/polymaker/ev/.ev_alerts_sent.json`. Never put webhook URLs in git.

On an **existing** instance (user-data does not re-run), after putting the parameters:

```bash
# append X_* / DISCORD_* into /etc/polymaker.env, rebuild/pull the image, then:
systemctl restart polymaker-monitor
# next polymaker-sharp.timer / polymaker-ev.timer run picks up /etc/polymaker.env automatically
```

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

On boot, user-data pulls `/polymaker/*` into `/etc/polymaker.env`, starts the monitor, and enables the sharp (`:00/:30`) and NFL EV (`:15/:45`) timers.

## Logs (CloudWatch)

Container stdout/stderr from the monitor, sharp pipeline, and NFL EV pipeline go to log group **`/polymaker/trading-bot`** in **eu-west-1**.

- Monitor stream: `monitor`
- Sharp pipeline stream: `sharp`
- NFL EV pipeline stream: `ev`

Console: https://eu-west-1.console.aws.amazon.com/cloudwatch/home?region=eu-west-1#logsV2:log-groups/log-group/$252Fpolymaker$252Ftrading-bot

You do not need a Session Manager shell to read these. Local `docker-compose.local.yml` still uses json-file logs and does not send to CloudWatch.

## 4. First-boot checks (SSM Session Manager)

```bash
aws ssm start-session --target <instance-id> --region eu-west-1
docker exec -it polymaker-monitor uv run polymaker doctor
journalctl -u polymaker-monitor -f
journalctl -u polymaker-sharp -f
journalctl -u polymaker-ev -f
systemctl list-timers polymaker-sharp.timer polymaker-ev.timer
```

On an **existing** instance (user-data does not re-run), after git pull / image rebuild:

```bash
mkdir -p /var/lib/polymaker/ev
cp /opt/polymaker/infra/systemd/polymaker-ev.service /opt/polymaker/infra/systemd/polymaker-ev.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now polymaker-ev.timer
# optional first run; watch CloudWatch stream `ev` or:
journalctl -u polymaker-ev -f
```

Local check (from `trading-bot/`; keep `POLYMAKER_LIVE` unset for dry-run):

```bash
uv run python scripts/run_nfl_ev_pipeline.py --trade both --min-edge-pct 5
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
- NCAAF: DraftKings, VSiN, SportsBettingDime (Pinnacle is not published for CFB; TheSpread/EVA supply RLM)

EVA / Covers are enrichment only and never block trading.
