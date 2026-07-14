# Acquisition monitoring

`valuechain-monitor` is the operational health monitor for the raw filing
acquisition pipeline. It is separate from `valuechain-global monitor`, which
polls disclosure sources and emits deduplicated filing events.

The health monitor checks:

- Postgres connectivity and expected acquisition source registration.
- Due issuer/filing/checkpoint backlog and the latest completed document time.
- Issuer claims that have remained `running` beyond the configured limit.
- Recent database document paths against files physically present on disk.
- SEC, CNINFO, and ESEF systemd service state.
- HDD capacity, warning below 5% free and critical below 2% free by default.

Every run atomically updates `latest.json` and appends a daily JSONL history in
`VALUECHAIN_MONITOR_REPORT_DIR`. Set `VALUECHAIN_MONITOR_WEBHOOK_URL` to send
warning or critical reports to an HTTP webhook. Repeated identical alerts are
suppressed for six hours by default.

Run once:

```bash
valuechain-monitor
```

Install the Cosmos timer:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/valuechain-acquisition-monitor.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now valuechain-acquisition-monitor.timer
systemctl --user start valuechain-acquisition-monitor.service
```

Inspect status and the latest report:

```bash
systemctl --user status valuechain-acquisition-monitor.timer
journalctl --user -u valuechain-acquisition-monitor.service -n 100 --no-pager
jq . /mnt/hdd8tb/valuechain/reports/acquisition-monitor/latest.json
```
