# Grafana Dashboards

## Quick start: "I got an alert with `dashboard_uid` -- what do I do?"

1. Find `dashboard_uid` in the alert's annotations (visible in Alertmanager or the runbook).
2. Open Grafana and navigate: **Dashboards > Search** (or press `/`), then search by the dashboard title from the table below.
3. If provisioned via `docker-compose.observability.yml`, dashboards appear in the **Grinder** folder.

## Where dashboards live in this repo

| Path | Purpose |
|------|---------|
| `monitoring/grafana/dashboards/*.json` | Dashboard definitions (auto-provisioned) |
| `monitoring/grafana/provisioning/dashboards/dashboard.yml` | Provisioning config (maps JSON files into Grafana) |
| `monitoring/grafana/provisioning/datasources/datasource.yml` | Prometheus datasource config |

## Dashboard UID mapping (SSOT)

| `dashboard_uid` | Title | JSON file | Used by alerts | Notes |
|-----------------|-------|-----------|----------------|-------|
| `grinder-overview` | Grinder Overview | `grinder_overview.json` | 6 alerts | System health, HA, gating, risk |
| `grinder-trading-loop` | Grinder Trading Loop | `grinder_trading_loop.json` | 5 alerts | Engine, SOR, fill-prob, order flow |
| `grinder-reconcile` | Grinder Reconcile & Remediation | `grinder_reconcile.json` | 1 alert | Reconcile loop, mismatches, budget |
| `grinder-ml-overview` | Grinder ML Overview | `grinder_ml_overview.json` | 1 alert | ML inference, latency, block reasons |
| `prometheus-targets` | Prometheus Targets | *(built-in)* | 2 alerts | Built-in Prometheus UI at `/targets`; no JSON in this repo |
| `grinder-ml-latency` | Grinder ML Latency & SLO | `grinder_ml_latency.json` | 0 alerts | ML latency histograms and SLO tracking |
| `grinder-ml-blocks` | Grinder ML Block Reasons | `grinder_ml_blocks.json` | 0 alerts | ML block reason breakdown |

The `dashboard_uid` values used in `monitoring/alert_rules.yml` are validated by `scripts/verify_alert_rules.py` (OBS-4).
The SSOT enum lives in `scripts/verify_alert_rules.py:DASHBOARD_UID_ENUM`.

> **SSOT note:** ML metrics, SLO targets, and panel details live in `docs/OBSERVABILITY_SLOS.md` — do not duplicate here.

## How to find a dashboard JSON by UID

```bash
# All dashboard UIDs in this directory:
grep -rn '"uid":' monitoring/grafana/dashboards/*.json | grep -v '"prometheus"'

# Specific UID lookup:
grep -rn '"uid": "grinder-overview"' monitoring/grafana/dashboards/
```

## How to find which alerts use a dashboard

```bash
grep -n 'dashboard_uid:' monitoring/alert_rules.yml
```

## Import / provisioning

Dashboards are auto-loaded via Grafana provisioning when using Docker Compose:

```bash
docker compose -f docker-compose.observability.yml up -d
```

To import manually: Grafana UI > Dashboards > Import > upload JSON file > select `prometheus` datasource.

All dashboards use the `prometheus` datasource UID. If your Prometheus datasource has a different UID, update the JSON files or use Grafana's import variable mapping.
