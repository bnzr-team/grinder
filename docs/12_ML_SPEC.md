# GRINDER - ML Specification

> Machine learning for parameter calibration and policy discovery

---

## 12.1 ML Philosophy

```
┌─────────────────────────────────────────────────────────────────┐
│                      ML IN GRINDER                               │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ML is NOT for:                                                 │
│  ❌ Predicting price direction                                  │
│  ❌ Timing entries/exits                                        │
│  ❌ Black-box trading decisions                                 │
│                                                                  │
│  ML IS for:                                                     │
│  ✓ Calibrating grid parameters                                  │
│  ✓ Optimizing policy thresholds                                 │
│  ✓ Discovering new policy rules (offline)                       │
│  ✓ Estimating fill probabilities                                │
│  ✓ Predicting toxicity regimes                                  │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 12.2 ML Use Cases

### Use Case 1: Parameter Calibration

**Goal**: Find optimal grid parameters (spacing, levels, sizes) for current market regime.

```python
@dataclass
class CalibrationTarget:
    """What we're optimizing for."""
    metric: str  # "sharpe", "rt_expectancy", "fill_rate"
    constraints: dict[str, tuple[float, float]]  # {"max_dd": (0, 0.05)}

class ParameterCalibrator:
    """Calibrate grid parameters using ML."""

    def __init__(self, model: BaseEstimator):
        self.model = model
        self.feature_names: list[str] = []

    def fit(self, historical_data: pd.DataFrame) -> None:
        """
        Fit calibration model.

        Features: market regime features
        Target: optimal parameters from walk-forward
        """
        X = historical_data[self.feature_names]
        y = historical_data["optimal_spacing"]  # From walk-forward

        self.model.fit(X, y)

    def predict_parameters(self, features: dict) -> dict[str, float]:
        """Predict optimal parameters for current conditions."""
        X = pd.DataFrame([features])[self.feature_names]
        predictions = self.model.predict(X)

        return {
            "spacing_bps": float(predictions[0]),
            # ... other parameters
        }
```

### Use Case 2: Toxicity Prediction

**Goal**: Predict toxicity regime transitions before they happen.

```python
class ToxicityPredictor:
    """Predict toxicity regime changes."""

    def __init__(self):
        self.model = LGBMClassifier(
            n_estimators=100,
            max_depth=5,
            learning_rate=0.1,
        )
        self.classes = ["LOW", "MID", "HIGH"]

    def fit(self, X: pd.DataFrame, y: pd.Series) -> None:
        """Train on historical toxicity transitions."""
        self.model.fit(X, y)

    def predict_proba(self, features: dict) -> dict[str, float]:
        """Predict probability of each toxicity regime."""
        X = pd.DataFrame([features])
        probs = self.model.predict_proba(X)[0]
        return dict(zip(self.classes, probs))

    def predict_transition(self, current_regime: str,
                           features: dict) -> tuple[str, float]:
        """Predict most likely next regime and confidence."""
        probs = self.predict_proba(features)
        next_regime = max(probs, key=probs.get)
        confidence = probs[next_regime]

        return next_regime, confidence
```

### Use Case 3: Fill Probability Model

**Goal**: Estimate probability of limit order filling within time horizon.

```python
class FillProbabilityModel:
    """ML model for fill probability estimation."""

    def __init__(self):
        self.model = LGBMRegressor(
            n_estimators=50,
            max_depth=4,
        )

    def fit(self, orders: pd.DataFrame) -> None:
        """
        Train on historical order outcomes.

        Features:
        - distance_from_mid_bps
        - spread_bps
        - ofi_zscore
        - depth_imbalance
        - volatility

        Target: did_fill (0/1) or time_to_fill
        """
        features = [
            "distance_bps", "spread_bps", "ofi_zscore",
            "depth_imbalance", "natr_14_5m"
        ]
        X = orders[features]
        y = orders["filled"]

        self.model.fit(X, y)

    def predict(self, order_price: float, mid: float,
                features: dict) -> float:
        """Predict fill probability."""
        distance_bps = abs(order_price - mid) / mid * 10000

        X = pd.DataFrame([{
            "distance_bps": distance_bps,
            "spread_bps": features["spread_bps"],
            "ofi_zscore": features.get("ofi_zscore", 0),
            "depth_imbalance": features.get("depth_imbalance", 0),
            "natr_14_5m": features["natr_14_5m"],
        }])

        return float(np.clip(self.model.predict(X)[0], 0, 1))
```

### Use Case 4: Policy Discovery (Offline)

**Goal**: Discover new policy rules from historical data.

```python
class PolicyDiscovery:
    """Discover optimal policy rules from data."""

    def __init__(self):
        self.tree = DecisionTreeClassifier(
            max_depth=5,
            min_samples_leaf=100,
        )

    def discover_rules(self, data: pd.DataFrame) -> list[PolicyRule]:
        """
        Discover decision rules for policy selection.

        Features: market conditions
        Target: best performing policy
        """
        X = data[self.feature_names]
        y = data["best_policy"]

        self.tree.fit(X, y)

        # Extract rules
        rules = self._extract_rules(self.tree)

        # Validate rules
        validated_rules = [
            rule for rule in rules
            if self._validate_rule(rule, data)
        ]

        return validated_rules

    def _extract_rules(self, tree: DecisionTreeClassifier) -> list[PolicyRule]:
        """Extract human-readable rules from decision tree."""
        rules = []
        tree_rules = export_text(tree, feature_names=self.feature_names)

        # Parse tree rules into PolicyRule objects
        # ...

        return rules
```

---

## 12.3 Feature Engineering for ML

```python
class MLFeatureEngine:
    """Generate features for ML models."""

    def __init__(self):
        self.scalers: dict[str, StandardScaler] = {}

    def compute_features(self, raw_features: dict,
                         lookback_features: list[dict]) -> dict:
        """
        Compute ML features from raw features.

        Includes:
        - Current values
        - Rolling statistics
        - Rate of change
        - Cross-feature interactions
        """
        features = {}

        # Current values (normalized)
        for key, value in raw_features.items():
            features[f"{key}_current"] = self._normalize(key, value)

        # Rolling statistics
        if len(lookback_features) >= 10:
            for key in raw_features.keys():
                values = [f.get(key, 0) for f in lookback_features[-10:]]
                features[f"{key}_mean_10"] = np.mean(values)
                features[f"{key}_std_10"] = np.std(values)
                features[f"{key}_trend"] = self._calc_trend(values)

        # Rate of change
        if len(lookback_features) >= 2:
            prev = lookback_features[-2]
            for key in raw_features.keys():
                if key in prev and prev[key] != 0:
                    features[f"{key}_roc"] = (raw_features[key] - prev[key]) / prev[key]

        # Interactions
        features["spread_x_vol"] = (
            features.get("spread_bps_current", 0) *
            features.get("natr_14_5m_current", 0)
        )
        features["ofi_x_depth"] = (
            features.get("ofi_zscore_current", 0) *
            features.get("depth_imbalance_current", 0)
        )

        return features
```

---

## 12.4 Model Training Pipeline

```python
class MLTrainingPipeline:
    """End-to-end ML training pipeline."""

    def __init__(self, config: MLConfig):
        self.config = config

    def train(self, data_path: Path) -> TrainedModel:
        """Full training pipeline."""

        # 1. Load data
        data = self._load_data(data_path)

        # 2. Feature engineering
        features = self._engineer_features(data)

        # 3. Train/val/test split (temporal)
        train, val, test = self._temporal_split(features)

        # 4. Train model with hyperparameter tuning
        model = self._train_with_tuning(train, val)

        # 5. Evaluate on test set
        metrics = self._evaluate(model, test)

        # 6. SHAP analysis for interpretability
        shap_values = self._compute_shap(model, test)

        # 7. Generate report
        report = self._generate_report(model, metrics, shap_values)

        return TrainedModel(
            model=model,
            metrics=metrics,
            shap_values=shap_values,
            report=report,
            trained_at=datetime.now(),
            data_hash=self._hash_data(data),
        )

    def _train_with_tuning(self, train: pd.DataFrame,
                           val: pd.DataFrame) -> BaseEstimator:
        """Train with Optuna hyperparameter tuning."""

        def objective(trial):
            params = {
                "n_estimators": trial.suggest_int("n_estimators", 50, 200),
                "max_depth": trial.suggest_int("max_depth", 3, 8),
                "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.3),
                "min_child_samples": trial.suggest_int("min_child_samples", 10, 100),
            }

            model = LGBMRegressor(**params)
            model.fit(
                train[self.feature_cols],
                train[self.target_col],
                eval_set=[(val[self.feature_cols], val[self.target_col])],
                callbacks=[early_stopping(50)],
            )

            preds = model.predict(val[self.feature_cols])
            return mean_squared_error(val[self.target_col], preds)

        study = optuna.create_study(direction="minimize")
        study.optimize(objective, n_trials=self.config.n_trials)

        # Train final model with best params
        best_model = LGBMRegressor(**study.best_params)
        best_model.fit(train[self.feature_cols], train[self.target_col])

        return best_model
```

---

## 12.5 Model Registry

```python
class ModelRegistry:
    """Registry for trained ML models."""

    def __init__(self, storage_path: Path):
        self.storage_path = storage_path
        self.metadata_file = storage_path / "registry.json"
        self.metadata: dict = self._load_metadata()

    def register(self, model: TrainedModel, name: str,
                 version: str) -> str:
        """Register a trained model."""
        model_id = f"{name}_{version}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        # Save model
        model_path = self.storage_path / model_id
        model_path.mkdir(parents=True)
        joblib.dump(model.model, model_path / "model.joblib")

        # Save metadata
        self.metadata[model_id] = {
            "name": name,
            "version": version,
            "trained_at": model.trained_at.isoformat(),
            "metrics": model.metrics,
            "data_hash": model.data_hash,
            "status": "staged",  # staged -> production -> deprecated
        }
        self._save_metadata()

        return model_id

    def load(self, model_id: str) -> BaseEstimator:
        """Load a registered model."""
        model_path = self.storage_path / model_id / "model.joblib"
        return joblib.load(model_path)

    def promote_to_production(self, model_id: str) -> None:
        """Promote model to production."""
        # Demote current production model
        for mid, meta in self.metadata.items():
            if meta.get("status") == "production":
                meta["status"] = "deprecated"

        self.metadata[model_id]["status"] = "production"
        self._save_metadata()

    def get_production_model(self, name: str) -> BaseEstimator | None:
        """Get current production model."""
        for model_id, meta in self.metadata.items():
            if meta["name"] == name and meta["status"] == "production":
                return self.load(model_id)
        return None
```

---

## 12.6 Model Monitoring

```python
class ModelMonitor:
    """Monitor model performance in production."""

    def __init__(self, model_id: str):
        self.model_id = model_id
        self.predictions: list[tuple[dict, float, float]] = []  # (features, pred, actual)

    def record_prediction(self, features: dict,
                          prediction: float,
                          actual: float | None = None) -> None:
        """Record a prediction for monitoring."""
        self.predictions.append((features, prediction, actual))

    def check_drift(self) -> DriftReport:
        """Check for feature drift and prediction drift."""
        if len(self.predictions) < 100:
            return DriftReport(has_drift=False, details={})

        # Feature drift (compare recent vs historical)
        recent_features = [p[0] for p in self.predictions[-100:]]
        historical_features = [p[0] for p in self.predictions[:-100]]

        drift_scores = {}
        for feature in recent_features[0].keys():
            recent_vals = [f[feature] for f in recent_features]
            hist_vals = [f[feature] for f in historical_features]

            # KS test for drift
            ks_stat, p_value = ks_2samp(recent_vals, hist_vals)
            if p_value < 0.05:
                drift_scores[feature] = ks_stat

        # Prediction accuracy drift
        recent_with_actual = [p for p in self.predictions[-100:] if p[2] is not None]
        if recent_with_actual:
            recent_errors = [abs(p[1] - p[2]) for p in recent_with_actual]
            hist_errors = [abs(p[1] - p[2]) for p in self.predictions[:-100] if p[2] is not None]
            if hist_errors:
                accuracy_drift = np.mean(recent_errors) / np.mean(hist_errors) - 1

        return DriftReport(
            has_drift=len(drift_scores) > 0 or accuracy_drift > 0.2,
            feature_drift=drift_scores,
            accuracy_drift=accuracy_drift,
        )
```

---

## 12.7 Calibration Path

```
┌─────────────────────────────────────────────────────────────────┐
│                    CALIBRATION PATH                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  1. OFFLINE BACKTEST                                            │
│     └── Walk-forward optimization                               │
│     └── Parameter sweep                                         │
│     └── Generate calibration dataset                            │
│                                                                  │
│  2. SHADOW MODE                                                 │
│     └── Run ML predictions alongside rule-based                 │
│     └── Compare performance (no real trades)                    │
│     └── Collect prediction accuracy data                        │
│                                                                  │
│  3. CANARY DEPLOYMENT                                           │
│     └── Small allocation (1-5% of capital)                      │
│     └── Monitor real performance vs shadow                      │
│     └── A/B test old vs new parameters                          │
│                                                                  │
│  4. PRODUCTION ROLLOUT                                          │
│     └── Gradual increase in allocation                          │
│     └── Continuous monitoring                                   │
│     └── Automated rollback on degradation                       │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 12.8 Model Configuration

```yaml
# config/ml.yaml
ml:
  enabled: true

  calibration:
    model: "lightgbm"
    retrain_interval_days: 7
    min_training_samples: 10000

  toxicity_prediction:
    model: "lightgbm"
    lookback_windows: [10, 30, 60]
    prediction_horizon_s: 60

  fill_probability:
    model: "lightgbm"
    features:
      - "distance_bps"
      - "spread_bps"
      - "ofi_zscore"
      - "depth_imbalance"
      - "natr_14_5m"

  monitoring:
    drift_check_interval_hours: 1
    accuracy_threshold: 0.7
    drift_threshold: 0.2

  registry:
    path: "models/"
    max_versions: 10
```
