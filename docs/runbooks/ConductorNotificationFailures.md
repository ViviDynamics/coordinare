# Runbook: CoordinareNotificationFailures

## Trigger Condition

Alert fires when `increase(coordinare_notifications_failed_total[5m]) > 0` — at least one notification has failed after all retry attempts within the last 5 minutes.

## Operational Impact

Operators are not receiving alerts for card events (PR merges, blocks, daemon restarts). Time-sensitive events such as blocked cards may go unnoticed, increasing mean time to resolution.

## Diagnostic Steps

1. Check `coordinare_notifications_failed_total{channel_name=...}` in `/metrics` to identify which channel (Slack or email) is failing. Cross-reference with `coordinare_notifications_rate_limited_total` to distinguish rate-limit suppression from delivery failure.
2. Verify the Slack webhook URL or SMTP credentials are still valid by checking the notification channel config: run `coordinare config validate` and look for `[DEPRECATED]` warnings or `[WRONG_TYPE]` errors on `notifications.channels[*]` fields.
3. Check the daemon logs for `notification_delivery_failed` log entries — the `error` field will contain the HTTP response or SMTP error that caused the failure. Common causes: expired Slack webhook (403), SMTP auth failure (535), network timeout to mail relay.

## Resolution Actions

1. For a Slack webhook failure (403), regenerate the webhook URL in Slack app settings, update `config.yaml` under `notifications.channels`, and restart the daemon. For SMTP auth failure, rotate the SMTP credentials in `config.yaml` or the corresponding `COORDINARE_*` env var and restart.

## Escalation Path

Contact the platform team (Slack: `#platform-oncall`) if the channel itself is unavailable (e.g., Slack is down) or if credentials cannot be rotated immediately. Provide: (1) the failing channel name from `/metrics`, (2) the error message from daemon logs, (3) the last timestamp of a successful notification. Expected response time: 30 minutes during business hours.
