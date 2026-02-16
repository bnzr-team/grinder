# Grinder Grafana Dashboards

## Available Dashboards

| Dashboard | UID | Description |
|-----------|-----|-------------|
| [Grinder Overview](grinder_overview.json) | `grinder-overview` | System health, HA status, kill-switch, gating |
| [Reconcile](grinder_reconcile.json) | `grinder-reconcile` | Reconciliation loop, mismatches, remediation |
| [ML Overview](grinder_ml_overview.json) | `grinder-ml-overview` | ML inference overview: mode, rates, latency, errors |
| [ML Latency & SLO](grinder_ml_latency.json) | `grinder-ml-latency` | Latency percentiles, heatmap, SLO compliance |
| [ML Block Reasons](grinder_ml_blocks.json) | `grinder-ml-blocks` | ACTIVE mode block reasons, distribution |

## Import Instructions

### Option 1: Provisioning (Recommended)

Dashboards are automatically loaded via Grafana provisioning when using Docker Compose:

```bash
docker-compose up -d grafana
```

Provisioning config: `monitoring/grafana/provisioning/dashboards/dashboard.yml`

### Option 2: Manual Import

1. Open Grafana UI (default: http://localhost:3000)
2. Go to Dashboards > Import
3. Upload JSON file or paste contents
4. Select `prometheus` datasource

## ML Dashboards (M8-02e)

### Metrics Used

```
# Counters
grinder_ml_inference_total         # Successful inferences
grinder_ml_inference_errors_total  # Inference errors
grinder_ml_block_total{reason}     # ACTIVE mode blocks by reason

# Histogram
grinder_ml_inference_latency_ms{mode}  # Latency by mode (shadow/active)
  - Buckets: 1, 5, 10, 25, 50, 100, 250, 500, 1000ms

# Gauges
grinder_ml_active_on               # 1 if ACTIVE mode, 0 if SHADOW
```

### SLO Targets

| Metric | SLO | Alert Threshold |
|--------|-----|-----------------|
| p99 latency | < 100ms | > 100ms for 5m |
| p99.9 latency | < 250ms | > 250ms for 3m |
| Error rate | < 5% | > 5% for 5m |

### Panel Reference

**ML Overview:**
- ML Mode stat (SHADOW/ACTIVE)
- Inference rate timeseries
- Latency percentiles by mode
- Block reasons stacked timeseries

**ML Latency & SLO:**
- p50/p95/p99/p99.9 stats
- Latency heatmap
- SLO compliance percentage

**ML Block Reasons:**
- Block distribution pie chart
- Block reasons table
- Mode vs block activity correlation

## Datasource

All dashboards use `prometheus` datasource UID. If your Prometheus datasource has a different UID, update the JSON files or use Grafana's import variable mapping.
