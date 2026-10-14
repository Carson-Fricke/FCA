//! Reusable study-family compositions. They never implement an analysis method.
use super::{Dataset, Panel, ResearchView};
use std::collections::BTreeMap;

/// A semantic slot and field-role requirement for a registered panel.
pub struct PanelRecipe {
    pub title: &'static str,
    pub kind: &'static str,
    pub slot: &'static str,
    pub encodings: &'static [(&'static str, &'static str)],
}

/// Built-in recipes are examples of the same open composition contract.
pub fn recipe(family: &str) -> Result<Vec<PanelRecipe>, String> {
    let panel = |title, kind, slot, encodings| PanelRecipe {
        title,
        kind,
        slot,
        encodings,
    };
    Ok(match family {
        "wavelets" => vec![
            panel(
                "Time × scale energy",
                "heatmap",
                "energy",
                &[("x", "time"), ("y", "scale"), ("value", "energy")],
            ),
            panel(
                "Price context",
                "line",
                "price",
                &[("x", "time"), ("y", "price")],
            ),
            panel(
                "Scale composition",
                "composition",
                "sessions",
                &[
                    ("x", "fine"),
                    ("y", "mid"),
                    ("z", "coarse"),
                    ("color", "outcome"),
                ],
            ),
        ],
        "seasonality" => vec![
            panel(
                "Session clock",
                "heatmap",
                "calendar",
                &[("x", "minute"), ("y", "session"), ("value", "value")],
            ),
            panel(
                "Aligned intraday paths",
                "line",
                "paths",
                &[("x", "minute"), ("y", "price"), ("series", "session")],
            ),
        ],
        "events" => vec![
            panel(
                "Confirmed events",
                "events",
                "events",
                &[("x", "time"), ("y", "price")],
            ),
            panel(
                "Duration and magnitude",
                "scatter",
                "events",
                &[("x", "duration"), ("y", "magnitude")],
            ),
            panel(
                "Event-aligned paths",
                "line",
                "paths",
                &[("x", "relative_time"), ("y", "price"), ("series", "event")],
            ),
        ],
        "regimes" => vec![
            panel(
                "Regime membership",
                "intervals",
                "states",
                &[("x", "start"), ("end", "end"), ("y", "state")],
            ),
            panel(
                "Published transitions",
                "heatmap",
                "transitions",
                &[("x", "from"), ("y", "to"), ("value", "probability")],
            ),
            panel(
                "Published dwell distribution",
                "distribution",
                "dwell",
                &[("x", "duration"), ("y", "count")],
            ),
        ],
        "predictive_signals" => vec![
            panel(
                "Signal and outcome",
                "scatter",
                "observations",
                &[("x", "signal"), ("y", "outcome")],
            ),
            panel(
                "Published horizon profile",
                "line",
                "horizons",
                &[("x", "horizon"), ("y", "estimate")],
            ),
            panel(
                "Published calibration",
                "comparison",
                "calibration",
                &[("x", "bucket"), ("y", "observed")],
            ),
            panel(
                "Published temporal stability",
                "line",
                "stability",
                &[("x", "time"), ("y", "estimate")],
            ),
        ],
        "liquidity" => vec![
            panel("Price", "line", "quotes", &[("x", "time"), ("y", "price")]),
            panel(
                "Spread",
                "line",
                "quotes",
                &[("x", "time"), ("y", "spread")],
            ),
            panel(
                "Activity",
                "line",
                "quotes",
                &[("x", "time"), ("y", "activity")],
            ),
            panel(
                "Published cost distribution",
                "distribution",
                "costs",
                &[("x", "cost"), ("y", "count")],
            ),
        ],
        "parameters" => vec![
            panel(
                "Joint experiment landscape",
                "heatmap",
                "experiments",
                &[
                    ("x", "parameter_x"),
                    ("y", "parameter_y"),
                    ("value", "outcome"),
                ],
            ),
            panel(
                "Executed and untested experiments",
                "table",
                "experiments",
                &[],
            ),
        ],
        "falsification" => vec![
            panel(
                "Claim and test evidence",
                "graph",
                "claims",
                &[("source", "claim"), ("target", "test")],
            ),
            panel(
                "Published null distribution",
                "distribution",
                "null",
                &[("x", "statistic"), ("y", "count")],
            ),
            panel(
                "Published negative controls",
                "comparison",
                "controls",
                &[("x", "control"), ("y", "observed")],
            ),
            panel(
                "Published subsample comparisons",
                "comparison",
                "subsamples",
                &[("x", "sample"), ("y", "estimate")],
            ),
        ],
        "learned_models" => vec![
            panel(
                "Training and evaluation windows",
                "intervals",
                "windows",
                &[("x", "start"), ("end", "end"), ("y", "scope")],
            ),
            panel(
                "Published feature profiles",
                "comparison",
                "features",
                &[("x", "feature"), ("y", "weight")],
            ),
            panel("Published diagnostics", "table", "diagnostics", &[]),
            panel(
                "Predictions and observations",
                "scatter",
                "predictions",
                &[("x", "prediction"), ("y", "observed")],
            ),
        ],
        _ => return Err(format!("unknown research recipe {family}")),
    })
}

/// Compose a family using explicit slot and field-role mappings supplied by the author.
/// `fields` keys are `slot.role` (e.g. `energy.time` -> `available_at_ns`).
pub fn compose(
    family: &str,
    title: &str,
    datasets: BTreeMap<String, Dataset>,
    fields: BTreeMap<String, String>,
) -> Result<ResearchView, String> {
    let mut panels = Vec::new();
    for (index, template) in recipe(family)?.into_iter().enumerate() {
        let dataset = datasets
            .get(template.slot)
            .ok_or_else(|| format!("missing evidence slot {}", template.slot))?;
        let encodings = template
            .encodings
            .iter()
            .map(|(channel, role)| {
                let key = format!("{}.{}", template.slot, role);
                fields
                    .get(&key)
                    .cloned()
                    .map(|field| ((*channel).to_string(), field))
                    .ok_or_else(|| format!("missing field mapping {key}"))
            })
            .collect::<Result<_, _>>()?;
        panels.push(Panel {
            id: format!("{family}-{index}"),
            kind: template.kind.into(),
            title: template.title.into(),
            dataset: dataset.id.clone(),
            encodings,
            description: None,
        });
    }
    let view = ResearchView {
        schema_version: 1,
        title: title.into(),
        description: String::new(),
        limitations: Vec::new(),
        datasets: datasets.into_values().collect(),
        panels,
    };
    view.validate()?;
    Ok(view)
}
