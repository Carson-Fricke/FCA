---
id: autoencoder-anomaly-detection
title: Autoencoder anomaly detection
version: 1
created: 2026-10-06
retrieved: 2026-10-06
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: c9f0c2521fb54d9a84296c62f2b4be322b0fa634837e00032359dff6f8ed1a71
tags:
  - learned-representations
  - anomaly-detection
  - autoencoder
  - feature-construction
  - machine-learning
  - model-card
collections:
  - model-cards
links:
  - volume-and-volatility-conditions
  - overfitting-and-selection-discipline
  - exploratory-search-and-multiple-attempts
  - model-selection-by-hypothesis-shape
  - realized-volatility-and-vol-forecasting
relationships:
  - type: relates
    target: volume-and-volatility-conditions
  - type: relates
    target: overfitting-and-selection-discipline
  - type: relates
    target: exploratory-search-and-multiple-attempts
  - type: relates
    target: model-selection-by-hypothesis-shape
  - type: relates
    target: realized-volatility-and-vol-forecasting
trust:
  tier: curated
  state: published
---
# Autoencoder anomaly detection

An autoencoder is a baseline for learned representations: compress a window
of market features through a bottleneck and score the window by how badly
the decoder reconstructs it. The score is an anomaly *condition*. It is not
a direction, and it is not a profit claim; a separate economic hypothesis
and directional rule are required. Simpler reconstruction baselines should
be tried first.

## In brief

- What it builds: a learned, low-dimensional representation of a feature
  window and a reconstruction-error score.
- Baseline architecture: a small encoder-decoder trained to reconstruct
  windows of normalized market features.
- Critical rules: fit preprocessing and weights only on the training
  partition; inference is causal; freeze weights before evaluation; never
  calibrate the anomaly threshold on validation or holdout data.
- An anomaly is a condition, not a direction or an edge.
- The learned model must beat or complement simple baselines (a volatility
  z-score or a PCA residual) or it should not be used.

## The method

Build fixed-length windows from features such as returns, range, realized
volatility, and volume. Normalize each feature with statistics computed on
the training partition only, then train a small autoencoder to minimize
reconstruction error on the training partition:

\[
\mathcal{L} =
\frac{1}{N}\sum_{i=1}^{N} \left\| x_i - \hat{x}_i \right\|^2 .
\]

The reconstruction error \(e_i = \|x_i - \hat{x}_i\|^2\) becomes the anomaly
score. A window is flagged when \(e_i\) exceeds a threshold. The threshold
is calibrated on a *declared calibration partition* — a slice separate from
training and from the final validation or holdout — so that the flag rate is
a stated choice rather than a fitted parameter hiding in the evaluation.

The causal and partitioning rules are the core of the method:

- Train preprocessing and weights on the training partition only; a scaler
  or normalization computed over the whole sample leaks the future.
- Inference must be causal: the score at time \(t\) may use only data
  available at \(t\), and the window construction must respect
  availability time (see [Historical timing and
  leakage](historical-timing-and-leakage)).
- Freeze the weights and the calibration threshold before touching
  validation or holdout data.
- Never tune architecture, threshold, or features on the validation or
  holdout set. If the validation set guides a choice, it is training data
  and the holdout must be rebuilt.

Training discipline: keep the architecture small relative to the sample,
fix every seed (initialization, shuffles, any dropout), use early stopping
on a declared validation split, and report convergence diagnostics such as
the loss curve and the distribution of reconstruction errors. Unseeded
randomness makes a result unreproducible and invites accidental selection.

**Anomaly is a condition, not an edge.** A high reconstruction error says
a window is unusual relative to the training period. It can be a
volatility spike, a structural break, or a data error (compare
[Volume and volatility conditions](volume-and-volatility-conditions) and
[Realized volatility and vol forecasting](realized-volatility-and-vol-forecasting)).
Turning an anomaly into a strategy requires an explicit directional rule
and a separate economic test.

**Nonstationarity and overfitting.** The definition of "normal" drifts, so
a model fit in one regime will flag the next one wholesale. Rolling or
expanding retraining can address this only if the retraining schedule is
itself causal and declared; and each refit is a researcher decision that
belongs in the attempt count. Reconstruction models can memorize noise, and
leakage can enter through scalers, window overlap, or threshold choice.

The platform does not provide an anomaly-detection or representation-
learning primitive today. Running this method requires authoring custom
study and training code; these cards describe the method and its causal
rules, not platform tooling.

## Simplest useful test

Compare the learned reconstruction score against two simple baselines on the
same windows, partitions, and evaluation:

- a trailing volatility z-score, and
- a PCA reconstruction residual, which is a linear autoencoder and a
  natural ablation of the learned representation.

The comparison metric should be declared before looking: for example, the
separation between flagged and unflagged forward outcomes, or the
conditional forward-return profile of the flagged windows. The learned model
must beat or complement the baseline to justify its complexity; if it does
not, report the baseline. A minimal experiment is a single training run on a
declared training partition, a fixed-seed model, a threshold calibrated on
the calibration partition only, and one untouched evaluation window with a
separate directional hypothesis tested at confirmation time.

## Statistical test

Report an uncertainty interval for whichever comparison metric is declared,
using a block bootstrap over time because windows overlap and volatility
clusters. Run a shuffled or time-shifted control: window labels or
time order are perturbed and the pipeline rerun; predictive value that
survives only in the unshuffled data is weak evidence, while value that
appears under the control is a bug. Check score-distribution stability
across sub-periods and regimes as a falsifier of nonstationarity. Because
architecture, window length, bottleneck size, and threshold are choices,
count them and apply selection discipline; see [Overfitting and selection
discipline](overfitting-and-selection-discipline) and [Exploratory search
and multiple attempts](exploratory-search-and-multiple-attempts).

## Economic test

The anomaly score is an entry condition, so build the economic test around
the paired directional and holding rule: apply a one-bar delay, charge the
measured spread and fees on every entry and exit, and report break-even
spread, turnover, and capacity. The learned model adds value only if the
resulting rule beats the same rule conditioned on the simple baseline, after
costs. A score that only identifies high-volatility periods is likely to
raise turnover more than it improves net return, and should be compared
against volatility-conditioned baselines explicitly.

## Limitations

- Anomaly is not direction: the score orders unusualness, not expected
  return.
- Nonstationarity: the training notion of normal decays and can flag whole
  regimes.
- Overfitting: small samples and flexible decoders memorize noise.
- Leakage risk: scalers, window normalization, and threshold calibration
  are common leak paths.
- Threshold sensitivity: conclusions can depend on the declared flag rate.
- Implementation cost: custom study and training code is required today.

## Related

- [Volume and volatility conditions](volume-and-volatility-conditions)
- [Overfitting and selection discipline](overfitting-and-selection-discipline)
- [Exploratory search and multiple attempts](exploratory-search-and-multiple-attempts)
- [Model selection by hypothesis shape](model-selection-by-hypothesis-shape)
- [Realized volatility and vol forecasting](realized-volatility-and-vol-forecasting)
