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
                .map_err(|e| e.to_string())
        })
        .collect::<Result<std::collections::BTreeMap<_, _>, _>>()?;
    let mut descriptors = std::collections::BTreeMap::new();
    for output in decoded.values() {
        if let crate::StudyOutput::DatasetSemantics { semantics } = output {
            if descriptors
                .insert(&semantics.dataset.output, &semantics.dataset)
                .is_some()
            {
                return Err("each output has one canonical dataset semantics descriptor".into());
            }
            semantics.validate_outputs(&decoded)?;
        }
    }
    for output in decoded.values() {
        output.validate()?;
        if let crate::StudyOutput::ResearchView { view } = output {
            view.validate_outputs(&decoded)?;
            for dataset in &view.datasets {
                if descriptors
                    .get(&dataset.output)
                    .is_some_and(|canonical| *canonical != dataset)
                {
                    return Err("research view conflicts with canonical dataset semantics".into());
                }
            }
        }
    }
    Ok(())
}
