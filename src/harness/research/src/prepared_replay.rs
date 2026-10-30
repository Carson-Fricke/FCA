//! Owns disposable, content-addressed tick-block storage, never market acquisition.
//! Immutable source pins and decoder identity select one current format. Each
//! block is checksummed before exposure; only complete verified streams publish.
//! Memory is bounded by a block. Contended cold writers bypass caching rather
//! than holding different input locks while waiting for chronological heads.
use harness_contracts::sha256::{digest, digest_hex};
use harness_trading::market::{TickBlock, TickKind, TickRow, MAX_BLOCK_ROWS, MAX_BLOCK_TEXT_BYTES};
use harness_trading::replay_errors::BacktestError;
use num::Decimal18;
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::PathBuf;
use std::sync::Arc;

const MAGIC: &[u8; 8] = b"FCATICK1";
const MAX_PAYLOAD: usize = MAX_BLOCK_TEXT_BYTES + MAX_BLOCK_ROWS * 104 + 4096;

#[derive(Clone, Debug)]
pub struct PreparedReplayConfig {
    pub directory: PathBuf,
    pub disk_bytes: u64,
}
impl PreparedReplayConfig {
    pub fn from_env() -> Result<Self, BacktestError> {
        Ok(Self {
            directory: std::env::var_os("HARNESS_PREPARED_REPLAY_DIR")
                .map_or_else(|| PathBuf::from(".local/prepared-replay"), PathBuf::from),
            disk_bytes: std::env::var("HARNESS_PREPARED_REPLAY_BYTES")
                .map_or(Ok(32 * 1024 * 1024 * 1024), |n| n.parse().map_err(error))?,
        })
    }
}
#[derive(Clone)]
pub(crate) struct Cache {
    config: PreparedReplayConfig,
}
impl Cache {
    pub fn new(config: PreparedReplayConfig) -> Self {
        Self { config }
    }
    pub fn key(&self, identity: &[u8]) -> String {
        // Changes to normalization or block semantics never reuse stale prepared
        // bytes. These are compile-time inputs, not mutable runtime source reads.
        let mut hash = harness_contracts::sha256::Sha256::new();
        for bytes in [
            MAGIC.as_slice(),
            include_bytes!("prepared_replay.rs").as_slice(),
            include_bytes!("market_replay.rs").as_slice(),
            include_bytes!("acquisition.rs").as_slice(),
            include_bytes!("../../marketdata/src/replay.rs").as_slice(),
            include_bytes!("../../marketdata/src/events.rs").as_slice(),
            include_bytes!("../../trading/src/market.rs").as_slice(),
            include_bytes!("../../num/src/decimal18.rs").as_slice(),
            include_bytes!("../../../lake/contracts/src/symbol.rs").as_slice(),
            include_bytes!("../../../lake/contracts/src/time.rs").as_slice(),
            identity,
        ] {
            hash.update(&(bytes.len() as u64).to_le_bytes());
            hash.update(bytes);
        }
        hash.finalize_hex()
    }
    pub fn open(&self, key: &str) -> Result<Option<Reader>, BacktestError> {
        if self.config.disk_bytes == 0 {
            return Ok(None);
        }
        let mut file = match File::open(self.config.directory.join(format!("{key}.ticks"))) {
            Ok(file) => file,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(error(e)),
        };
        let mut header = [0u8; 40];
        file.read_exact(&mut header).map_err(error)?;
        if &header[..8] != MAGIC
            || digest_hex(key.as_bytes()) != harness_contracts::sha256::hex(&header[8..])
        {
            return Err(error("outdated or mismatched prepared replay header"));
        }
        Ok(Some(Reader {
            file,
            rows: 0,
            finished: false,
            buffer: Vec::new(),
        }))
    }
    pub fn writer(&self, key: &str) -> Result<Option<Writer>, BacktestError> {
        if self.config.disk_bytes == 0 {
            return Ok(None);
        }
        fs::create_dir_all(self.config.directory.join("locks")).map_err(error)?;
        let lock = File::options()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(self.config.directory.join("locks").join(key))
            .map_err(error)?;
        match lock.try_lock() {
            Ok(()) => {}
            Err(std::fs::TryLockError::WouldBlock) => return Ok(None),
            Err(e) => return Err(error(e)),
        }
        // A writer may have published between the initial read and try_lock.
        if self.config.directory.join(format!("{key}.ticks")).exists() {
            return Ok(None);
        }
        let prefix = format!("{key}.partial-");
        for entry in fs::read_dir(&self.config.directory).map_err(error)? {
            let entry = entry.map_err(error)?;
            if entry.file_name().to_string_lossy().starts_with(&prefix) {
                // This key's OS lock proves that any earlier writer exited.
                fs::remove_file(entry.path()).map_err(error)?;
            }
        }
        let mut file = tempfile::Builder::new()
            .prefix(&prefix)
            .tempfile_in(&self.config.directory)
            .map_err(error)?;
        file.write_all(MAGIC).map_err(error)?;
        file.write_all(&digest(key.as_bytes())).map_err(error)?;
        Ok(Some(Writer {
            file: Some(file),
            _lock: lock,
            cache: self.clone(),
            key: key.into(),
            rows: 0,
            bytes: 40,
            buffer: Vec::new(),
        }))
    }
}
pub(crate) struct Writer {
    file: Option<tempfile::NamedTempFile>,
    _lock: File,
    cache: Cache,
    key: String,
    rows: u64,
    bytes: u64,
    buffer: Vec<u8>,
}
impl Writer {
    pub fn push(&mut self, block: &TickBlock) -> Result<(), BacktestError> {
        let Some(file) = &mut self.file else {
            return Ok(());
        };
        encode(block, &mut self.buffer)?;
        self.bytes += 36 + self.buffer.len() as u64;
        if self.bytes + 12 > self.cache.config.disk_bytes {
            self.file.take(); // Oversized streams still execute, without retention.
            return Ok(());
        }
        file.write_all(&(self.buffer.len() as u32).to_le_bytes())
            .map_err(error)?;
        file.write_all(&digest(&self.buffer)).map_err(error)?;
        file.write_all(&self.buffer).map_err(error)?;
        self.rows += block.rows().len() as u64;
        Ok(())
    }
    pub fn finish(mut self) -> Result<(), BacktestError> {
        let Some(mut file) = self.file.take() else {
            return Ok(());
        };
        file.write_all(&0u32.to_le_bytes()).map_err(error)?;
        file.write_all(&self.rows.to_le_bytes()).map_err(error)?;
        file.flush().map_err(error)?;
        // Admission/eviction is short and never holds a replay channel. Readers
        // retain opened files; Windows sharing failures simply prevent eviction.
        let admission = File::options()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(self.cache.config.directory.join("locks/admission"))
            .map_err(error)?;
        admission.lock().map_err(error)?;
        let mut entries = Vec::new();
        for entry in fs::read_dir(&self.cache.config.directory).map_err(error)? {
            let entry = entry.map_err(error)?;
            if entry.path().extension().is_some_and(|ext| ext == "ticks") {
                let meta = entry.metadata().map_err(error)?;
                entries.push((meta.modified().map_err(error)?, entry.path(), meta.len()));
            }
        }
        entries.sort_by_key(|e| e.0);
        let mut retained: u64 = entries.iter().map(|e| e.2).sum();
        for (_, path, bytes) in entries {
            if retained + self.bytes + 12 <= self.cache.config.disk_bytes {
                break;
            }
            if fs::remove_file(path).is_ok() {
                retained -= bytes;
            }
        }
        if retained + self.bytes + 12 <= self.cache.config.disk_bytes {
            file.persist_noclobber(
                self.cache
                    .config
                    .directory
                    .join(format!("{}.ticks", self.key)),
            )
            .map_err(error)?;
        }
        Ok(())
    }
}
pub(crate) struct Reader {
    file: File,
    rows: u64,
    finished: bool,
    buffer: Vec<u8>,
}
impl Iterator for Reader {
    type Item = Result<Arc<TickBlock>, BacktestError>;
    fn next(&mut self) -> Option<Self::Item> {
        if self.finished {
            return None;
        }
        let result = (|| {
            let mut length = [0u8; 4];
            self.file.read_exact(&mut length).map_err(error)?;
            let length = u32::from_le_bytes(length) as usize;
            if length == 0 {
                let mut count = [0u8; 8];
                self.file.read_exact(&mut count).map_err(error)?;
                let mut trailing = [0];
                if u64::from_le_bytes(count) != self.rows
                    || self.file.read(&mut trailing).map_err(error)? != 0
                {
                    return Err(error("prepared replay footer mismatch"));
                }
                return Ok(None);
            }
            if length > MAX_PAYLOAD {
                return Err(error("prepared replay block exceeds capacity"));
            }
            let mut checksum = [0u8; 32];
            self.file.read_exact(&mut checksum).map_err(error)?;
            self.buffer.resize(length, 0);
            self.file.read_exact(&mut self.buffer).map_err(error)?;
            if digest(&self.buffer) != checksum {
                return Err(error("prepared replay checksum mismatch"));
            }
            let block = Arc::new(decode(&self.buffer)?);
            self.rows += block.rows().len() as u64;
            Ok(Some(block))
        })();
        match result {
            Ok(Some(block)) => Some(Ok(block)),
            Ok(None) => {
                self.finished = true;
                None
            }
            Err(e) => {
                self.finished = true;
                Some(Err(e))
            }
        }
    }
}
fn encode(block: &TickBlock, out: &mut Vec<u8>) -> Result<(), BacktestError> {
    out.clear();
    out.push(if block.kind() == TickKind::Quote {
        0
    } else {
        1
    });
    for n in [block.symbol().len(), block.rows().len(), block.text().len()] {
        out.extend((n as u32).to_le_bytes());
    }
    out.extend(block.symbol().as_bytes());
    out.extend(block.text().as_bytes());
    let numbers = if block.kind() == TickKind::Quote {
        4
    } else {
        2
    };
    for row in block.rows() {
        out.extend(row.timestamp_ns.to_le_bytes());
        out.extend(row.sequence.to_le_bytes());
        for value in &row.values[..numbers] {
            out.extend(value.unscaled().to_le_bytes());
        }
        for (start, len) in [row.event_id, row.venue, row.trade_id] {
            out.extend(start.to_le_bytes());
            out.extend(len.to_le_bytes());
        }
    }
    if out.len() > MAX_PAYLOAD {
        return Err(error("prepared replay block exceeds capacity"));
    }
    Ok(())
}
fn decode(mut input: &[u8]) -> Result<TickBlock, BacktestError> {
    let kind = match take::<1>(&mut input)?[0] {
        0 => TickKind::Quote,
        1 => TickKind::Trade,
        _ => return Err(error("invalid prepared tick kind")),
    };
    let symbol_len = u32::from_le_bytes(take(&mut input)?) as usize;
    let rows = u32::from_le_bytes(take(&mut input)?) as usize;
    let text_len = u32::from_le_bytes(take(&mut input)?) as usize;
    if symbol_len > 4096 || rows == 0 || rows > MAX_BLOCK_ROWS || text_len > MAX_BLOCK_TEXT_BYTES {
        return Err(error("invalid prepared replay dimensions"));
    }
    let symbol = take_text(&mut input, symbol_len)?;
    let text = take_text(&mut input, text_len)?;
    let numbers = if kind == TickKind::Quote { 4 } else { 2 };
    if input.len() != rows * (40 + numbers * 16) {
        return Err(error("prepared replay row length mismatch"));
    }
    let mut output = Vec::with_capacity(rows);
    for _ in 0..rows {
        let timestamp_ns = i64::from_le_bytes(take(&mut input)?);
        let sequence = u64::from_le_bytes(take(&mut input)?);
        let mut values = [Decimal18::ZERO; 4];
        for value in &mut values[..numbers] {
            *value =
                Decimal18::from_unscaled(i128::from_le_bytes(take(&mut input)?)).map_err(error)?;
        }
        let mut range = || -> Result<_, BacktestError> {
            Ok((
                u32::from_le_bytes(take(&mut input)?),
                u32::from_le_bytes(take(&mut input)?),
            ))
        };
        output.push(TickRow {
            timestamp_ns,
            sequence,
            values,
            event_id: range()?,
            venue: range()?,
            trade_id: range()?,
        });
    }
    TickBlock::new(symbol, kind, output, text).map_err(error)
}
fn take<const N: usize>(input: &mut &[u8]) -> Result<[u8; N], BacktestError> {
    let (head, tail) = input
        .split_at_checked(N)
        .ok_or_else(|| error("truncated prepared replay"))?;
    *input = tail;
    Ok(head.try_into().expect("checked length"))
}
fn take_text(input: &mut &[u8], len: usize) -> Result<String, BacktestError> {
    let (head, tail) = input
        .split_at_checked(len)
        .ok_or_else(|| error("truncated prepared text"))?;
    let text = std::str::from_utf8(head).map_err(error)?.to_string();
    *input = tail;
    Ok(text)
}
fn error(message: impl std::fmt::Display) -> BacktestError {
    BacktestError::DataPreflight(format!("prepared replay: {message}"))
}
