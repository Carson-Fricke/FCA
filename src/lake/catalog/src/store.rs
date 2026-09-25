//! Indexed local JSON catalog store.
//!
//! Replaces Python's whole-file `local/catalog.json` document with an
//! in-memory index loaded once at `open`. Reads never re-parse the file;
//! writes merge into the index and atomically rewrite the document with the
//! exact bytes Python's `json.dumps(..., ensure_ascii=True,
//! separators=(",", ":"), sort_keys=True) + "\n"` produces, so existing local
//! state and the Python CLI stay byte-compatible.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::fmt::Write as _;
use std::fs::{self, File, OpenOptions};
use std::io::{ErrorKind, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{Duration, UNIX_EPOCH};

use lake_contracts::{canonical_symbol, AssetClass, DataError, DataType, Date};

use crate::models::{coverage_from_value, CoverageRecord};
use crate::ports::{CatalogRepository, CoverageQuery};

/// The catalog schema version Python writes and this store accepts.
const SCHEMA_VERSION: u64 = 1;

/// How many times an atomic replace is retried when Windows denies it because
/// another handle briefly holds the target open.
const REPLACE_ATTEMPTS: usize = 6;

/// Base backoff for a denied replace: `50ms * attempt`.
const REPLACE_BACKOFF: Duration = Duration::from_millis(50);

static TEMPORARY_COUNTER: AtomicU64 = AtomicU64::new(0);

type CoverageKey = (AssetClass, DataType, String, Date);

#[derive(Debug, Default)]
struct StoreState {
    stamp: Option<(u64, u64)>,
    index: BTreeMap<CoverageKey, Arc<CoverageRecord>>,
}

/// Coverage catalog persisted as stable, atomically replaced JSON.
///
/// `open` reads an existing `catalog.json` once into an in-memory index; a
/// missing file is an empty store. `get` and `list` answer from the index,
/// `upsert` merges into it and rewrites the file once, and `reload` picks up a
/// file changed by another process.
#[derive(Debug)]
pub struct JsonCatalogStore {
    path: Option<PathBuf>,
    state: Mutex<StoreState>,
}

impl JsonCatalogStore {
    /// Open an existing `catalog.json` or start an empty store at `path`.
    ///
    /// The file must have schema version 1, exactly the keys `schema_version`
    /// and `coverage`, and coverage rows with exactly the ten canonical keys;
    /// duplicate logical keys are rejected. A missing file is an empty store.
    pub fn open(path: impl Into<PathBuf>) -> Result<Self, DataError> {
        let path = path.into();
        let mut state = StoreState::default();
        refresh(&path, &mut state)?;
        Ok(Self {
            path: Some(path),
            state: Mutex::new(state),
        })
    }

    /// An empty store that never touches the filesystem.
    pub fn in_memory() -> Self {
        Self {
            path: None,
            state: Mutex::new(StoreState::default()),
        }
    }

    /// The backing file, or `None` for an in-memory store.
    pub fn path(&self) -> Option<&Path> {
        self.path.as_deref()
    }

    /// Re-read the backing file if its size or modification time changed.
    pub fn reload(&self) -> Result<(), DataError> {
        let Some(path) = self.path.clone() else {
            return Ok(());
        };
        let mut state = lock(&self.state);
        refresh(&path, &mut state)
    }

    /// Replace every supplied logical key in one catalog-file update.
    ///
    /// Duplicate keys inside `records` are rejected, mirroring Python's
    /// `upsert_many`.
    pub fn upsert_many<'a, I>(&self, records: I) -> Result<(), DataError>
    where
        I: IntoIterator<Item = &'a CoverageRecord>,
    {
        let updates: Vec<&CoverageRecord> = records.into_iter().collect();
        let mut keys = BTreeSet::new();
        for record in &updates {
            if !keys.insert(record.key()) {
                return Err(DataError::value("coverage update contains duplicate keys"));
            }
        }
        let Some(path) = self.path.clone() else {
            let mut state = lock(&self.state);
            for record in updates {
                state.index.insert(record.key(), Arc::new(record.clone()));
            }
            return Ok(());
        };
        let mut state = lock(&self.state);
        refresh(&path, &mut state)?;
        let mut replaced = Vec::with_capacity(updates.len());
        for record in &updates {
            let key = record.key();
            let previous = state.index.insert(key.clone(), Arc::new((*record).clone()));
            replaced.push((key, previous));
        }
        let document = serialize_document(state.index.values().map(AsRef::as_ref));
        if let Err(error) = write_atomic(&path, document.as_bytes()) {
            for (key, previous) in replaced {
                match previous {
                    Some(previous) => {
                        state.index.insert(key, previous);
                    }
                    None => {
                        state.index.remove(&key);
                    }
                }
            }
            return Err(error);
        }
        if let Some(stamp) = file_stamp(&path) {
            state.stamp = Some(stamp);
        }
        Ok(())
    }
}

impl CatalogRepository for JsonCatalogStore {
    fn get(
        &self,
        asset_class: AssetClass,
        data_type: DataType,
        symbol: &str,
        session_date: Date,
    ) -> Result<Option<CoverageRecord>, DataError> {
        let symbol = canonical_symbol(symbol)?;
        let state = self.answer()?;
        Ok(state
            .index
            .get(&(asset_class, data_type, symbol, session_date))
            .map(|record| (**record).clone()))
    }

    fn upsert(&self, record: &CoverageRecord) -> Result<(), DataError> {
        self.upsert_many(std::iter::once(record))
    }

    fn list(&self, query: &CoverageQuery) -> Result<Vec<CoverageRecord>, DataError> {
        let symbol = query.symbol.as_deref().map(canonical_symbol).transpose()?;
        if let (Some(start), Some(end)) = (query.start, query.end) {
            if start > end {
                return Err(DataError::value("start must be on or before end"));
            }
        }
        let state = self.answer()?;
        let mut matches: Vec<CoverageRecord> = state
            .index
            .values()
            .filter(|record| {
                query
                    .asset_class
                    .is_none_or(|value| value == record.asset_class)
                    && query
                        .data_type
                        .is_none_or(|value| value == record.data_type)
                    && symbol
                        .as_deref()
                        .is_none_or(|value| value == record.symbol.as_str())
                    && query.start.is_none_or(|start| record.session_date >= start)
                    && query.end.is_none_or(|end| record.session_date <= end)
            })
            .map(|record| (**record).clone())
            .collect();
        matches.sort_by(|left, right| left.sort_key().cmp(&right.sort_key()));
        Ok(matches)
    }
}

impl JsonCatalogStore {
    /// Take the state lock for a read, first noticing external writers.
    ///
    /// The check is one stat: a changed `(mtime_ns, size)` stamp re-parses the
    /// file (Python's warm-read behavior); an unchanged stamp answers from the
    /// index without touching the file contents.
    fn answer(&self) -> Result<MutexGuard<'_, StoreState>, DataError> {
        let Some(path) = self.path.clone() else {
            return Ok(lock(&self.state));
        };
        let mut state = lock(&self.state);
        refresh(&path, &mut state)?;
        Ok(state)
    }
}

fn lock(state: &Mutex<StoreState>) -> MutexGuard<'_, StoreState> {
    state
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// Re-read the file when its `(mtime_ns, size)` stamp changed.
///
/// A parse failure leaves the current index and stamp untouched so the store
/// keeps answering from the last good parse. An absent file clears the index.
fn refresh(path: &Path, state: &mut StoreState) -> Result<(), DataError> {
    let stamp = file_stamp(path);
    if stamp == state.stamp {
        return Ok(());
    }
    match stamp {
        None => {
            state.index.clear();
            state.stamp = None;
            Ok(())
        }
        Some(stamp) => {
            let index = load_index(path)?;
            state.index = index;
            state.stamp = Some(stamp);
            Ok(())
        }
    }
}

/// Return the file's `(mtime_ns, size)`, or `None` if it cannot be read.
fn file_stamp(path: &Path) -> Option<(u64, u64)> {
    let metadata = fs::metadata(path).ok()?;
    let modified = metadata.modified().ok()?;
    let nanos = modified.duration_since(UNIX_EPOCH).map_or(0, |elapsed| {
        u64::try_from(elapsed.as_nanos()).unwrap_or(u64::MAX)
    });
    Some((nanos, metadata.len()))
}

/// Parse and validate one catalog document into an index.
fn load_index(path: &Path) -> Result<BTreeMap<CoverageKey, Arc<CoverageRecord>>, DataError> {
    let text = fs::read_to_string(path).map_err(|error| invalid(path, error))?;
    let payload: serde_json::Value =
        serde_json::from_str(&text).map_err(|error| invalid(path, error))?;
    let object = payload
        .as_object()
        .ok_or_else(|| invalid_message(path, "catalog root has unexpected fields"))?;
    if object.len() != 2
        || !object.contains_key("schema_version")
        || !object.contains_key("coverage")
    {
        return Err(invalid_message(path, "catalog root has unexpected fields"));
    }
    if object["schema_version"].as_u64() != Some(SCHEMA_VERSION) {
        return Err(invalid_message(
            path,
            "catalog schema version is unsupported",
        ));
    }
    let rows = object["coverage"]
        .as_array()
        .ok_or_else(|| invalid_message(path, "catalog coverage must be a list"))?;
    let mut index = BTreeMap::new();
    for row in rows {
        let record = coverage_from_value(row).map_err(|error| invalid(path, error))?;
        if index.insert(record.key(), Arc::new(record)).is_some() {
            return Err(invalid_message(
                path,
                "catalog contains duplicate coverage keys",
            ));
        }
    }
    Ok(index)
}

fn invalid(path: &Path, error: impl fmt::Display) -> DataError {
    DataError::io(format!(
        "local catalog is invalid: {} ({error})",
        path.display()
    ))
}

fn invalid_message(path: &Path, message: &str) -> DataError {
    DataError::io(format!(
        "local catalog is invalid: {} ({message})",
        path.display()
    ))
}

/// Serialize the exact bytes Python's canonical `json.dumps` call produces.
fn serialize_document<'a>(records: impl IntoIterator<Item = &'a CoverageRecord>) -> String {
    let mut ordered: Vec<&CoverageRecord> = records.into_iter().collect();
    ordered.sort_by(|left, right| left.sort_key().cmp(&right.sort_key()));
    let mut document = String::from("{\"coverage\":[");
    for (position, record) in ordered.iter().enumerate() {
        if position > 0 {
            document.push(',');
        }
        write_row(&mut document, record);
    }
    document.push_str("],\"schema_version\":1}\n");
    document
}

fn write_row(out: &mut String, record: &CoverageRecord) {
    out.push('{');
    write_string_field(out, "asset_class", record.asset_class.value());
    out.push(',');
    write_number_field(out, "byte_size", record.byte_size);
    out.push(',');
    write_optional_string_field(out, "checksum", record.checksum.as_deref());
    out.push(',');
    write_string_field(out, "data_type", record.data_type.value());
    out.push(',');
    write_optional_string_field(out, "error_message", record.error_message.as_deref());
    out.push(',');
    write_optional_string_field(out, "object_path", record.object_path.as_deref());
    out.push(',');
    write_number_field(out, "row_count", record.row_count);
    out.push(',');
    write_string_field(out, "session_date", &record.session_date.isoformat());
    out.push(',');
    write_string_field(out, "status", record.status.value());
    out.push(',');
    write_string_field(out, "symbol", &record.symbol);
    out.push('}');
}

fn write_string_field(out: &mut String, key: &str, value: &str) {
    escape_json_into(out, key);
    out.push(':');
    escape_json_into(out, value);
}

fn write_optional_string_field(out: &mut String, key: &str, value: Option<&str>) {
    escape_json_into(out, key);
    out.push(':');
    match value {
        Some(value) => escape_json_into(out, value),
        None => out.push_str("null"),
    }
}

fn write_number_field(out: &mut String, key: &str, value: u64) {
    escape_json_into(out, key);
    out.push(':');
    let _ = write!(out, "{value}");
}

/// Escape a JSON string the way Python's `json.dumps(ensure_ascii=True)` does.
///
/// Unlike `serde_json`, Python escapes everything outside `U+0020..=U+007E`
/// as `\uXXXX` UTF-16 code units, and it does not escape a forward slash.
fn escape_json_into(out: &mut String, value: &str) {
    out.push('"');
    for character in value.chars() {
        match character {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            character if !('\u{20}'..='\u{7e}').contains(&character) => {
                let mut buffer = [0u16; 2];
                for unit in character.encode_utf16(&mut buffer) {
                    let _ = write!(out, "\\u{unit:04x}");
                }
            }
            character => out.push(character),
        }
    }
    out.push('"');
}

/// Write `bytes` to `path` atomically: temp file, flush, fsync, replace.
fn write_atomic(path: &Path, bytes: &[u8]) -> Result<(), DataError> {
    let parent = match path.parent() {
        Some(parent) if !parent.as_os_str().is_empty() => parent,
        _ => Path::new("."),
    };
    fs::create_dir_all(parent).map_err(DataError::io)?;
    let name = path
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_else(|| "catalog.json".to_string());
    let (temporary_path, mut temporary) = create_temporary(parent, &name)?;
    let write_result = temporary
        .write_all(bytes)
        .and_then(|()| temporary.flush())
        .and_then(|()| temporary.sync_all());
    drop(temporary);
    if let Err(error) = write_result {
        let _ = fs::remove_file(&temporary_path);
        return Err(DataError::io(error));
    }
    replace_with_retries(&temporary_path, path)
}

/// Create a uniquely named temporary file beside the catalog.
fn create_temporary(parent: &Path, name: &str) -> Result<(PathBuf, File), DataError> {
    for _ in 0..64 {
        let candidate = parent.join(format!(
            ".{name}.{}.{}.tmp",
            std::process::id(),
            TEMPORARY_COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        match OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&candidate)
        {
            Ok(file) => return Ok((candidate, file)),
            Err(error) if error.kind() == ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(DataError::io(error)),
        }
    }
    Err(DataError::io("could not create temporary catalog file"))
}

/// Atomically install the temporary file, retrying a transiently denied
/// replace (a Windows sharing violation) with `50ms * attempt` backoff.
fn replace_with_retries(temporary_path: &Path, path: &Path) -> Result<(), DataError> {
    let mut last_error = None;
    for attempt in 0..REPLACE_ATTEMPTS {
        match fs::rename(temporary_path, path) {
            Ok(()) => return Ok(()),
            Err(error) => {
                let retryable = error.kind() == ErrorKind::PermissionDenied;
                last_error = Some(error);
                if !retryable || attempt == REPLACE_ATTEMPTS - 1 {
                    break;
                }
                std::thread::sleep(REPLACE_BACKOFF * (attempt as u32 + 1));
            }
        }
    }
    let _ = fs::remove_file(temporary_path);
    match last_error {
        Some(error) => Err(DataError::io(error)),
        None => Err(DataError::io("could not replace the catalog file")),
    }
}

#[cfg(test)]
#[path = "../tests/unit/store.rs"]
mod tests;
