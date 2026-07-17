---
name: python-ml
description: Build, inspect, train, evaluate, and explain Python machine-learning workflows. Use for tabular data analysis, feature engineering, supervised or unsupervised learning, model comparison, validation, metrics, plots, reproducible experiments, and generating Python ML code.
---

# Python machine learning

Inspect the dataset before modeling: identify the target, feature types, missingness, duplicates,
class balance, time ordering, grouping, and leakage risks. Ask for the target or success metric only
when it cannot be inferred safely.

Build a reproducible pipeline:

1. Fix random seeds and record relevant package versions.
2. Split data before fitting transformations. Use time-based or group-based splits when required.
3. Fit preprocessing only on training data.
4. Start with an interpretable baseline.
5. Compare models using cross-validation appropriate to the data-generating process.
6. Evaluate on untouched holdout data with metrics suited to the objective and imbalance.
7. Report uncertainty, leakage checks, failure modes, and operational limitations.

Prefer `pandas`, `numpy`, and scikit-learn pipelines. Use `statsmodels` when statistical inference,
diagnostics, or classical time-series methods matter. Consider XGBoost or LightGBM for suitable
tabular prediction problems after establishing a simpler baseline. Use `matplotlib` or `seaborn`
for diagnostic plots. Avoid expensive searches unless justified; begin with a small, meaningful
parameter space. Never claim causality from predictive performance alone.

Save generated artifacts only when requested. Do not execute untrusted dataset content as code.
