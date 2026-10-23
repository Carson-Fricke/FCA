use harness_execution::jobs::{JobKind, JobReader, JobRecord, JobState};
use std::collections::BTreeMap;
pub struct StoredJob {
    pub record: JobRecord,
    pub artifacts: BTreeMap<String, Vec<u8>>,
}
pub trait JobInspection: JobReader {
    fn get(&self, id: &str) -> Option<JobRecord> {
        self.try_get(id).expect("fixture metadata read")
    }
    fn list(
        &self,
        kind: Option<JobKind>,
        state: Option<JobState>,
        strategy: Option<&str>,
    ) -> Vec<JobRecord> {
        self.try_list(kind, state, strategy)
            .expect("fixture metadata list")
    }
    fn fetch(&self, id: &str) -> Option<StoredJob> {
        let record = self.try_get(id).expect("fixture metadata read")?;
        let artifacts = self
            .artifacts(id)
            .expect("fixture inventory")?
            .into_iter()
            .map(|info| {
                let bytes = self
                    .artifact(id, &info.name)
                    .expect("fixture artifact read")
                    .expect("published fixture artifact");
                (info.name, bytes)
            })
            .collect();
        Some(StoredJob { record, artifacts })
    }
}
impl<T: JobReader + ?Sized> JobInspection for T {}
