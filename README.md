# App reviews to Slack

Polls App Store and Google Play reviews for DubDub every 30 minutes and posts new ones to Slack. Reviews rated 1–2★ are marked with :rotating_light:.

## Secrets (Settings → Secrets and variables → Actions)

| Secret | Value |
|---|---|
| `SLACK_WEBHOOK_URL` | Slack incoming webhook URL |
| `APPLE_APP_ID` | Numeric Apple ID from App Store Connect → App Information |
| `ASC_ISSUER_ID` | App Store Connect API issuer ID |
| `ASC_KEY_ID` | App Store Connect API key ID |
| `ASC_PRIVATE_KEY` | Full contents of the .p8 file, including the BEGIN/END lines |
| `GPLAY_SERVICE_ACCOUNT_JSON` | Full contents of the Google service account JSON key |

iOS runs only when `APPLE_APP_ID` is set. Android runs only when `GPLAY_SERVICE_ACCOUNT_JSON` is set.

## Behaviour

- The first run for each platform records existing reviews without posting them. Later runs post only new reviews.
- Seen review IDs are stored in `state.json`, which the workflow commits back to the repo.
- To post existing reviews on the first run, set `BACKFILL: "1"` in the workflow env.
- To run immediately: Actions → App reviews to Slack → Run workflow.

## Local test

```
pip install -r requirements.txt
export APPLE_APP_ID=... ASC_ISSUER_ID=... ASC_KEY_ID=... ASC_PRIVATE_KEY="$(cat AuthKey_XXXX.p8)"
export GPLAY_SERVICE_ACCOUNT_JSON="$(cat service-account.json)"
python reviews.py --dry-run
```
