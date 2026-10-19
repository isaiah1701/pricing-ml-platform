# Airbnb Nightly Price Prediction

## Business objective

Predict the advertised nightly price of a New York City Airbnb listing to
support host price recommendations and internal pricing benchmarks.

- Type: regression
- Entity/key: listing / `id`
- Target: `price_per_night` in US dollars
- FeatureService: `airbnb_price_model_v1`
- Primary metric: MAE
- Secondary metrics: RMSE and R²

The target is an advertised price, not booking revenue or occupancy. The model
therefore estimates market listing prices; it does not optimize host revenue.

## Dataset

The transformed dataset contains 48,884 listings. Its overall median price is
**$106**, compared with a mean of **$152.76**. The $10,000 maximum shows that
the price distribution has a long upper tail.

| Segment | Median nightly price |
|---|---:|
| Bronx | $65 |
| Brooklyn | $90 |
| Manhattan | $150 |
| Queens | $75 |
| Staten Island | $75 |
| Entire home/apt | $160 |
| Private room | $70 |
| Shared room | $45 |

Dataset SHA-256:

```text
0e0aa1a94f298abed85f6edbe7986e7fd975a1f4b836418533c1973327dd68ff
```

![New York City borough and neighbourhood coverage](../data/New_York_City_.png)

The map makes the model’s geographic scope clear: predictions should be used
for the New York City listing areas represented in the training data.

## Metric meaning

- **MAE:** typical prediction error in dollars. Lower is better and easiest to
  interpret from a business perspective.
- **RMSE:** penalizes large pricing mistakes more heavily. Lower is better.
- **R²:** proportion of price variation explained by the model. Higher is
  better; values near zero indicate weak explanatory power.

## Baseline results

All results use the same 80/20 split with `random_state=42`: 39,107 training
rows and 9,777 held-out test rows.

| Predictor | MAE ↓ | RMSE ↓ | R² ↑ |
|---|---:|---:|---:|
| Overall median | $82.73 | $205.10 | -0.051 |
| Neighbourhood median | $77.84 | $200.30 | -0.002 |
| Neighbourhood + room-type median | **$63.51** | $192.20 | 0.077 |
| Linear Regression | $71.27 | **$185.36** | **0.142** |
| Random Forest | $66.31 | $193.78 | 0.062 |
| KNN Regression | $69.08 | $197.72 | 0.024 |

## Experiment summary

The experiments moved from simple baselines to log-target models, gradient
boosting, room-type experts, and CatBoost. The final results are summarized
below; the individual iteration log is intentionally omitted.

| Model | MAE ↓ | RMSE ↓ | R² ↑ |
|---|---:|---:|---:|
| Overall median | $82.73 | $205.10 | -0.051 |
| Neighbourhood + room-type median | $63.51 | $192.20 | 0.077 |
| Log-target gradient boosting | $56.21 | $179.44 | 0.196 |
| Room-type experts | $55.48 | $177.55 | 0.213 |
| CatBoost | $54.78 | $176.97 | 0.218 |

## Final model

The three-seed CatBoost ensemble is the measured champion:

| Model | MAE ↓ | RMSE ↓ | R² ↑ |
|---|---:|---:|---:|
| **CatBoost ensemble** | **$54.67** | **$176.66** | **0.220** |

| Test segment | Rows | MAE ↓ | RMSE ↓ | R² ↑ |
|---|---:|---:|---:|---:|
| At or below $250 | 8,714 | $30.13 | $43.11 | 0.435 |
| Above $250 | 1,063 | $255.88 | $521.36 | -0.239 |
| Above $500 | 203 | $789.35 | $1,149.96 | -0.859 |
| At or below $500 | 9,574 | $39.10 | $61.90 | 0.513 |

The gain is incremental because model choice is approaching saturation on the
current columns. Expensive listings remain difficult: the inputs lack bedrooms,
bathrooms, guest capacity, amenities, property quality, seasonality, events,
competitor prices, and booking demand. The product should present a price range
and flag luxury listings as low-confidence.

The consolidated champion is trained with:

```bash
uv run python -m training.train
```

The canonical MLflow run contains the reload-verified model, feature
importance, and segment metrics. Next steps are to add richer property and
demand features, inference-time uncertainty, and a time-based test split.
