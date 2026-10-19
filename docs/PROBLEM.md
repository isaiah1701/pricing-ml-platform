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

## Metric meaning

- **MAE:** typical prediction error in dollars. Lower is better and easiest to
  interpret from a business perspective.
- **RMSE:** penalizes large pricing mistakes more heavily. Lower is better.
- **R²:** proportion of price variation explained by the model. Higher is
  better; values near zero indicate weak explanatory power.

## Baseline results

All results use the same 80/20 split with `random_state=42`: 39,107 training
rows and 9,777 held-out test rows.

Iterations 1–8 used 13 Feast features. Iteration 9 uses 18.

| Predictor | MAE ↓ | RMSE ↓ | R² ↑ |
|---|---:|---:|---:|
| Overall median | $82.73 | $205.10 | -0.051 |
| Neighbourhood median | $77.84 | $200.30 | -0.002 |
| Neighbourhood + room-type median | **$63.51** | $192.20 | 0.077 |
| Linear Regression | $71.27 | **$185.36** | **0.142** |
| Random Forest | $66.31 | $193.78 | 0.062 |
| KNN Regression | $69.08 | $197.72 | 0.024 |

### First model run versus median baselines

Compared with always predicting the overall median of $106, Random Forest
reduces MAE from $82.73 to $66.31—an improvement of **$16.42 (19.9%)**. Linear
Regression reduces RMSE from $205.10 to $185.36—an improvement of **$19.74
(9.6%)**. This confirms that the Feast features contain useful predictive
information beyond a single default price.

However, the simple neighbourhood and room-type median still has the best MAE:
$63.51 versus Random Forest's $66.31. The project's current value is therefore
the reproducible Feast-to-training-to-MLflow evaluation workflow and a measured
baseline for improvement, not a production pricing recommendation yet.

## Iteration 2: ordered room-type hypothesis

This iteration encoded room type as `shared < private < entire home`, while
minimum nights, availability, and location remained numeric. It learned a
strong positive room-type step and a negative minimum-night effect, but more
availability—not less—was associated with higher predicted price after
controlling for the other fields.

| Model | MAE ↓ | RMSE ↓ | R² ↑ |
|---|---:|---:|---:|
| Original Linear Regression | **$71.27** | **$185.36** | **0.142** |
| Ordered-room Linear Regression | $71.59 | $185.61 | 0.140 |

The hypothesis did not improve held-out performance. Forcing equal price steps
between room types is too restrictive: the observed median increase is $25
from shared to private rooms but $90 from private rooms to entire homes. The
one-hot room-type representation remains preferable.

## Iteration 3: log-price target

This iteration restored one-hot room types, excluded the duplicate
`availability_ratio`, and trained Linear Regression on `log1p(price_per_night)`.
Predictions are converted back to dollars before evaluation.

| Predictor | MAE ↓ | RMSE ↓ | R² ↑ |
|---|---:|---:|---:|
| Neighbourhood + room-type median | $63.51 | $192.20 | 0.077 |
| Original Linear Regression | $71.27 | **$185.36** | **0.142** |
| Log-target Linear Regression | **$59.74** | $186.53 | 0.131 |

The log target reduces MAE by **$3.78 (6.0%)** versus the strongest median
baseline and by **$11.53 (16.2%)** versus the original Linear Regression. It
also beats the median baseline on RMSE and R², making it the strongest current
candidate. It improves MAE for every room type and all neighbourhood groups
except the Bronx, where MAE increases from $29.19 to $30.80.

## Iteration 4: log-target gradient boosting

This iteration keeps the log target, one-hot categories, and deduplicated
availability input, but learns nonlinear thresholds and feature interactions
with histogram gradient boosting.

| Model | MAE ↓ | RMSE ↓ | R² ↑ |
|---|---:|---:|---:|
| Log-target Linear Regression | $59.74 | $186.53 | 0.131 |
| Log-target Gradient Boosting | **$56.21** | **$179.44** | **0.196** |

Gradient boosting improves all three metrics and is the strongest current
candidate. It lowers MAE for every room type, every price band, and four of five
neighbourhood groups. Staten Island is nearly unchanged ($59.98 to $60.26 MAE)
and has only 81 test listings, so it needs more data or uncertainty controls.
High-price listings remain the largest weakness at $264.75 MAE above $250.

## Iteration 5: gated standard and premium experts

This iteration uses a classifier to route likely prices above $250 to a
specialized premium model. A conservative 0.90 routing probability was selected
on validation data; soft blending performed worse and was rejected.

| Model | MAE ↓ | RMSE ↓ | R² ↑ |
|---|---:|---:|---:|
| Single Gradient Booster | **$56.21** | **$179.44** | **0.196** |
| Gated Price Regressor | $57.57 | $185.97 | 0.136 |

The gate has 77.8% premium precision but only 11.2% recall. It routes just 119
of 1,063 premium test listings correctly and misses 944. The standard expert
improves MAE from $30.77 to $27.53 for listings at or below $250, but missed
premium listings increase premium MAE from $264.75 to $303.86. The gated model
is rejected overall. It should be reconsidered only after adding property
features that can identify premium listings more reliably.

## Iteration 6: price-weighted gradient boosting

Training-only weights of 1×, 1.5×, and 2× were applied to prices at or below
$250, from $251–$500, and above $500. Three-fold cross-validation selected
absolute-error loss, a 0.05 learning rate, and 200 boosting iterations.

| Model | MAE ↓ | RMSE ↓ | R² ↑ |
|---|---:|---:|---:|
| Iteration 4 Gradient Booster | $56.21 | **$179.44** | **0.196** |
| Weighted Gradient Booster | **$55.71** | $182.42 | 0.169 |

Weighting lowers overall MAE by only $0.50. Above-$250 MAE improves from
$264.75 to $263.91, while above-$500 MAE worsens from $812.65 to $816.69.
Stronger weights improved premium validation error but materially worsened
overall error. The weighted model is an MAE challenger, but Iteration 4 remains
the balanced champion because it has better RMSE, R², and extreme-price error.

## Iteration 7: room-type experts

Three log-target Gradient Boosters were trained independently for shared rooms,
private rooms, and entire homes. Routing uses the known `room_type` input, so it
does not depend on an unreliable classifier.

| Model | MAE ↓ | RMSE ↓ | R² ↑ |
|---|---:|---:|---:|
| Single Gradient Booster | $56.21 | $179.44 | 0.196 |
| Room-type Experts | **$55.48** | **$177.55** | **0.213** |

| Room type | Single-model MAE | Expert MAE |
|---|---:|---:|
| Shared room | $36.87 | **$35.31** |
| Private room | $31.75 | **$31.12** |
| Entire home/apt | $78.98 | **$78.20** |

The specialists improve every aggregate metric and every room-type segment, so
Iteration 7 is the new champion. Entire homes remain the largest source of
room-type error. Shared-room results should be treated cautiously because that
expert has only 932 training rows.

## Iteration 8: tuned entire-home expert

Cross-validation selected direct-dollar absolute-error training with a 0.05
learning rate and 200 iterations for the entire-home expert. Private and shared
experts were unchanged.

| Model | MAE ↓ | RMSE ↓ | R² ↑ |
|---|---:|---:|---:|
| Iteration 7 Room-type Experts | $55.48 | **$177.55** | **0.213** |
| Tuned Entire-home Expert | **$55.13** | $182.94 | 0.164 |

Entire-home MAE improves from $78.20 to $77.52, but above-$250 MAE worsens from
$259.37 to $273.80 and above-$500 MAE worsens from $796.68 to $832.07. The
direct-dollar absolute-error objective favors common prices at the expense of
the expensive tail. Iteration 8 is the lowest-MAE challenger, while Iteration 7
remains the balanced champion.

## Iteration 9: enhanced features and blended entire-home expert

Five deterministic features were added through `transform.py` and Feast:
distance to Midtown, coordinate cell, minimum-stay band, availability band, and
neighbourhood/room-type interaction. Training-only validation selected a 40%
log-target and 60% direct-dollar blend for entire homes. Per-room isotonic
calibration worsened validation MAE and was rejected.

| Model | MAE ↓ | RMSE ↓ | R² ↑ |
|---|---:|---:|---:|
| Iteration 7 Balanced Experts | $55.48 | **$177.55** | **0.213** |
| Enhanced Blended Experts | **$54.98** | $180.27 | 0.188 |

Entire-home MAE improves from $78.20 to $77.13, but shared-room MAE worsens
from $35.31 to $36.14 and private-room MAE is nearly unchanged at $31.19.
At-or-below-$250 MAE improves to $29.14, while above-$250 MAE worsens from
$259.37 to $266.84 and above-$500 MAE worsens from $796.68 to $816.55. This is
the lowest-MAE challenger, while Iteration 7 remains the balanced champion.

## Decision after Iteration 10

1. Track median baselines in MLflow.
2. Log segment metrics and investigate high-price and Staten Island errors.
3. Add property, amenity, demand, and seasonality features when available.
4. Revisit premium weighting or gating only after adding those signals.

## Iteration 10: native categorical boosting

CatBoost was selected on an inner validation split and evaluated once on the
unchanged held-out test set. It learns categorical relationships directly and
keeps the log-price target that worked well in earlier iterations.

| Model | MAE ↓ | RMSE ↓ | R² ↑ |
|---|---:|---:|---:|
| Iteration 7 Balanced Experts | $55.48 | $177.55 | 0.213 |
| Iteration 10 CatBoost | **$54.78** | **$176.97** | **0.218** |

CatBoost is the new full-market champion, but the gain is incremental: MAE
falls by $0.70 and R² rises by 0.005. A training-only geographic-comparables
model was also tested and rejected (MAE $59.22, R² 0.109).

For the 97.9% of held-out listings priced at or below $500, CatBoost records
MAE **$39.17**, RMSE **$62.04**, and R² **0.511**. This is a diagnostic segment,
not the headline result: its membership uses the true target and therefore
cannot be used as an inference-time routing rule. Listings above $500 account
for only 2.1% of rows but still dominate full-market error.

The experiments now show that estimator changes are saturated. A major jump
requires property capacity, bedrooms, bathrooms, amenities, calendar/date,
events, competitor prices, and actual booking outcomes. Until those signals
exist, the product should present a price range and flag luxury listings as
low-confidence rather than promise exact full-market prices.

## Next iteration

1. Keep Iteration 10 as the measured model champion at this stage.
2. Add inference-time uncertainty and low-confidence warnings.
3. Acquire property, demand, calendar, and comparable-booking features.
4. Retrain with a time-based test split before a production decision.

## Iteration 11: neural-network benchmark

A validation search compared six MLP configurations with sparse one-hot
categories, scaled numeric inputs, a standardized log-price target, Adam,
ReLU, and early stopping on dollar MAE. A three-seed ensemble was selected.

| Model | MAE ↓ | RMSE ↓ | R² ↑ |
|---|---:|---:|---:|
| Iteration 10 CatBoost | **$54.78** | **$176.97** | **0.218** |
| Iteration 11 Neural ensemble | $56.47 | $180.84 | 0.183 |

The neural network overfit within a few epochs and was rejected. It confirms
that extra layers cannot recover property quality, capacity, amenities, or
demand signals absent from the dataset.

## Iteration 12: final CatBoost seed ensemble

Past iterations showed that log-price training consistently lowers typical
error, native categorical boosting produces the strongest overall model, and
direct-dollar objectives trade much worse MAE for only small tail improvements.
Validation therefore tested independent CatBoost seeds and selected the
three-seed average only after it improved MAE, RMSE, and R² together.

| Model | MAE ↓ | RMSE ↓ | R² ↑ |
|---|---:|---:|---:|
| Iteration 10 CatBoost | $54.78 | $176.97 | 0.218 |
| **Iteration 12 CatBoost ensemble** | **$54.67** | **$176.66** | **0.220** |

| Test segment | Rows | MAE ↓ | RMSE ↓ | R² ↑ |
|---|---:|---:|---:|---:|
| At or below $250 | 8,714 | $30.13 | $43.11 | 0.435 |
| Above $250 | 1,063 | $255.88 | $521.36 | -0.239 |
| Above $500 | 203 | $789.35 | $1,149.96 | -0.859 |
| At or below $500 | 9,574 | $39.10 | $61.90 | 0.513 |

Iteration 12 is the new measured champion because every headline metric
improved. The gain is small, which is consistent with the experiment history:
model choice is saturated on the current columns.

Expensive listings fail because price has a long tail up to $10,000 while the
inputs contain no bedrooms, bathrooms, guest capacity, amenities, property
quality, views, events, or booking demand. Listings with the same room type and
location can therefore have very different prices but appear almost identical
to the model. Log training correctly prevents those few rows from distorting
the other 97.9%, but it also pulls unexplained luxury prices toward the normal
market. Direct-price training pushes ordinary predictions upward and worsens
MAE, so it was rejected.

The consolidated champion is trained with:

```bash
uv run python -m training.train
```

Canonical MLflow run `4b4bf562e5cc4f3b9bbee1fc87e969c7` contains the
reload-verified model, feature importance, and segment metrics. Research run
`40d02b20700d42d08152b2a1d6ac6401` records the validation and promotion
decision, while neural run `26902ef510ff44a29ac3e4e49e780860` records the
rejected benchmark.
