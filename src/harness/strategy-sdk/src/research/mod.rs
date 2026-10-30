//! Declarative research exhibits over immutable, named study evidence.
mod publishing;
pub mod recipes;
mod view;

pub use publishing::{EvidenceTable, WaveletHistory};
pub use view::{Dataset, DatasetSemantics, Field, FieldType, Panel, ResearchView};

/// Resolve exhibit references only once the complete publication is available.
pub fn validate_publication(
    outputs: &std::collections::BTreeMap<String, serde_json::Value>,
) -> Result<(), String> {
    let decoded = outputs
        .iter()
        .map(|(name, value)| {
            serde_json::from_value::<crate::StudyOutput>(value.clone())
                .map(|output| (name.clone(), output))
                .map_err(|e| format!("research output {name:?}: {e}"))
        })
        .collect::<Result<std::collections::BTreeMap<_, _>, _>>()?;
    let mut descriptors = std::collections::BTreeMap::new();
    for (name, output) in &decoded {
        if let crate::StudyOutput::DatasetSemantics { semantics } = output {
            if descriptors
                .insert(&semantics.dataset.output, &semantics.dataset)
                .is_some()
            {
                return Err(format!(
                    "research semantics {name:?}: duplicate canonical descriptor for output {:?}",
                    semantics.dataset.output
                ));
            }
            semantics
                .validate_outputs(&decoded)
                .map_err(|e| format!("research semantics {name:?}: {e}"))?;
        }
    }
    for (name, output) in &decoded {
        output
            .validate()
            .map_err(|e| format!("research output {name:?}: {e}"))?;
        if let crate::StudyOutput::ResearchView { view } = output {
            view.validate_outputs(&decoded)
                .map_err(|e| format!("research view {name:?}: {e}"))?;
            for dataset in &view.datasets {
                if descriptors
                    .get(&dataset.output)
                    .is_some_and(|canonical| *canonical != dataset)
                {
                    return Err(format!("research view {name:?}, dataset {:?}: descriptor conflicts with canonical semantics for output {:?}", dataset.id, dataset.output));
                }
            }
        }
    }
    Ok(())
}
