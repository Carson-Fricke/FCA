//! Owns in-memory conditional objects for isolated compositions and tests.
//! It implements the same persistence port as local/cloud storage, not job policy.
use harness_execution::{
    jobs::StoreError,
    persistence::{error, Backend, Result},
};
use std::{collections::BTreeMap, sync::Mutex};
#[derive(Default)]
pub struct Memory {
    objects: Mutex<BTreeMap<String, Vec<u8>>>,
}
impl Backend for Memory {
    fn read(&self, path: &str) -> Result<Option<Vec<u8>>> {
        Ok(self.objects.lock().map_err(error)?.get(path).cloned())
    }
    fn open(&self, path: &str) -> Result<Box<dyn std::io::Read + Send>> {
        let bytes = self.read(path)?.ok_or_else(|| error("object missing"))?;
        Ok(Box::new(std::io::Cursor::new(bytes)))
    }
    fn create(&self, path: &str, bytes: &[u8]) -> Result<bool> {
        let mut objects = self.objects.lock().map_err(error)?;
        match objects.entry(path.into()) {
            std::collections::btree_map::Entry::Vacant(entry) => {
                entry.insert(bytes.to_vec());
                Ok(true)
            }
            std::collections::btree_map::Entry::Occupied(_) => Ok(false),
        }
    }
    fn create_file(&self, path: &str, source: &std::path::Path) -> Result<()> {
        self.create(path, &std::fs::read(source).map_err(error)?)?;
        Ok(())
    }
    fn update(&self, path: &str, change: &mut dyn FnMut(Vec<u8>) -> Result<Vec<u8>>) -> Result<()> {
        let mut objects = self.objects.lock().map_err(error)?;
        let bytes = objects
            .get(path)
            .ok_or_else(|| StoreError::JobNotFound(path.into()))?
            .clone();
        let next = change(bytes)?;
        objects.insert(path.into(), next);
        Ok(())
    }
    fn list(&self, prefix: &str) -> Result<Vec<String>> {
        Ok(self
            .objects
            .lock()
            .map_err(error)?
            .keys()
            .filter(|key| key.starts_with(prefix))
            .cloned()
            .collect())
    }
    fn delete(&self, path: &str) -> Result<()> {
        self.objects.lock().map_err(error)?.remove(path);
        Ok(())
    }
}
