//! Exact replay histories with bounded RAM and compressed, owned scratch files.
//!
//! This is execution scratch, not a persisted application format. All rows are
//! retained; statistics and Parquet output never require a full in-memory copy.

use std::fs::File;
use std::io;
use std::io::Read;
use std::io::Write;
use std::marker::PhantomData;
use std::sync::Arc;
use tempfile::NamedTempFile;

const CHUNK: usize = 8192;
const MAX_FRAME: usize = 64 * 1024 * 1024;

/// Caller-owned scratch allocation and capacity. The engine never reads environment settings.
#[derive(Clone, Default)]
pub struct HistoryResources {
    allocator: Option<Arc<dyn Fn() -> io::Result<NamedTempFile> + Send + Sync>>,
    max_bytes: u64,
}
impl std::fmt::Debug for HistoryResources {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("HistoryResources")
            .field("max_bytes", &self.max_bytes)
            .finish()
    }
}
impl HistoryResources {
    pub fn new(
        max_bytes: u64,
        allocator: Arc<dyn Fn() -> io::Result<NamedTempFile> + Send + Sync>,
    ) -> Self {
        Self {
            max_bytes,
            allocator: Some(allocator),
        }
    }
    fn allocate(&self) -> io::Result<NamedTempFile> {
        self.allocator
            .as_ref()
            .ok_or_else(|| io::Error::other("replay history requires caller-supplied scratch"))?(
        )
    }
}

pub trait Record: Clone {
    fn encode(&self, output: &mut Vec<u8>);
    fn decode(input: &mut &[u8]) -> io::Result<Self>;
}

#[derive(Debug)]
pub struct History<T: Record> {
    tail: Vec<T>,
    file: Option<Arc<NamedTempFile>>,
    count: usize,
    bytes: u64,
    last: Option<T>,
    resources: HistoryResources,
    writer: Option<FrameWriter>,
}

/// Reusable execution-local compression workspace, never shared by snapshots.
struct FrameWriter {
    raw: Vec<u8>,
    compressed: Vec<u8>,
    compressor: zstd::bulk::Compressor<'static>,
}
impl std::fmt::Debug for FrameWriter {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("FrameWriter").finish_non_exhaustive()
    }
}
impl FrameWriter {
    fn new() -> io::Result<Self> {
        let mut compressor = zstd::bulk::Compressor::new(1)?;
        compressor.set_parameter(zstd::zstd_safe::CParameter::ChecksumFlag(true))?;
        Ok(Self { raw: Vec::new(), compressed: Vec::new(), compressor })
    }
}
impl<T: Record> Clone for History<T> {
    fn clone(&self) -> Self {
        Self {
            tail: self.tail.clone(),
            file: self.file.clone(),
            count: self.count,
            bytes: self.bytes,
            last: self.last.clone(),
            resources: self.resources.clone(),
            writer: None,
        }
    }
}
impl<T: Record> Default for History<T> {
    fn default() -> Self {
        Self {
            tail: Vec::new(),
            file: None,
            count: 0,
            bytes: 0,
            last: None,
            resources: HistoryResources::default(),
            writer: None,
        }
    }
}
impl<T: Record> History<T> {
    pub fn new(resources: HistoryResources) -> Self {
        Self {
            resources,
            ..Self::default()
        }
    }
    pub fn resources(&self) -> HistoryResources {
        self.resources.clone()
    }
    pub fn push(&mut self, value: T) -> io::Result<()> {
        self.last = Some(value.clone());
        self.tail.push(value);
        self.count += 1;
        if self.tail.len() >= CHUNK {
            if self.writer.is_none() {
                self.writer = Some(FrameWriter::new()?);
            }
            let writer = self.writer.as_mut().unwrap();
            writer.raw.clear();
            for record in &self.tail {
                record.encode(&mut writer.raw);
            }
            if writer.raw.len() > MAX_FRAME {
                return Err(io::Error::other("replay history frame exceeds capacity"));
            }
            writer.compressed.clear();
            writer.compressed.reserve(zstd::zstd_safe::compress_bound(writer.raw.len()));
            writer.compressor.compress_to_buffer(&writer.raw, &mut writer.compressed)?;
            let bytes = self.bytes + writer.compressed.len() as u64 + 16;
            if bytes > self.resources.max_bytes {
                return Err(io::Error::other(
                    "replay history scratch capacity exhausted",
                ));
            }
            if self.file.is_none() {
                self.file = Some(Arc::new(self.resources.allocate()?));
            }
            // A completed history may be cloned by result consumers. Fork its
            // scratch before appending so each snapshot keeps its exact rows.
            if Arc::strong_count(self.file.as_ref().unwrap()) > 1 {
                let mut replacement = self.resources.allocate()?;
                let mut original = File::open(self.file.as_ref().unwrap().path())?.take(self.bytes);
                if io::copy(&mut original, &mut replacement)? != self.bytes {
                    return Err(io::Error::other("truncated replay history"));
                }
                self.file = Some(Arc::new(replacement));
            }
            let file = Arc::get_mut(self.file.as_mut().unwrap()).unwrap().as_file_mut();
            file.write_all(&(writer.compressed.len() as u64).to_le_bytes())?;
            file.write_all(&(writer.raw.len() as u64).to_le_bytes())?;
            file.write_all(&writer.compressed)?;
            self.bytes = bytes;
            self.tail.clear();
        }
        Ok(())
    }
    pub fn len(&self) -> usize {
        self.count
    }
    pub fn is_empty(&self) -> bool {
        self.count == 0
    }
    pub fn last(&self) -> Option<&T> {
        self.last.as_ref()
    }
    pub fn iter(&self) -> io::Result<impl Iterator<Item = io::Result<T>> + '_> {
        let file = self
            .file
            .as_ref()
            .map(|file| File::open(file.path()).map(|file| file.take(self.bytes)))
            .transpose()?;
        Ok(HistoryReader::<T> {
            file,
            batch: Vec::new().into_iter(),
            marker: PhantomData,
            failed: false,
            compressed: Vec::new(),
            raw: Vec::new(),
            decompressor: None,
        }
        .chain(self.tail.iter().cloned().map(Ok)))
    }
}
struct HistoryReader<T: Record> {
    file: Option<io::Take<File>>,
    batch: std::vec::IntoIter<T>,
    marker: PhantomData<T>,
    failed: bool,
    compressed: Vec<u8>,
    raw: Vec<u8>,
    decompressor: Option<zstd::bulk::Decompressor<'static>>,
}
impl<T: Record> Iterator for HistoryReader<T> {
    type Item = io::Result<T>;
    fn next(&mut self) -> Option<Self::Item> {
        if self.failed {
            return None;
        }
        if let Some(value) = self.batch.next() {
            return Some(Ok(value));
        }
        let file = self.file.as_mut()?;
        let load = (|| -> io::Result<Option<Vec<T>>> {
            let mut header = [0u8; 16];
            let n = file.read(&mut header[..1])?;
            if n == 0 {
                return Ok(None);
            }
            file.read_exact(&mut header[1..])?;
            let compressed = u64::from_le_bytes(header[..8].try_into().unwrap()) as usize;
            let size = u64::from_le_bytes(header[8..].try_into().unwrap()) as usize;
            if compressed > MAX_FRAME || size > MAX_FRAME {
                return Err(io::Error::other("invalid replay history frame"));
            }
            self.compressed.resize(compressed, 0);
            file.read_exact(&mut self.compressed)?;
            self.raw.clear();
            self.raw.reserve(size);
            if self.decompressor.is_none() {
                self.decompressor = Some(zstd::bulk::Decompressor::new()?);
            }
            let decoded = self.decompressor.as_mut().unwrap()
                .decompress_to_buffer(&self.compressed, &mut self.raw)?;
            if decoded != size {
                return Err(io::Error::other("invalid replay history frame size"));
            }
            let mut input = self.raw.as_slice();
            let mut records = Vec::with_capacity(CHUNK);
            while !input.is_empty() {
                records.push(T::decode(&mut input)?);
            }
            if records.len() != CHUNK {
                return Err(io::Error::other("truncated replay history frame"));
            }
            Ok(Some(records))
        })();
        match load {
            Ok(Some(records)) => {
                self.batch = records.into_iter();
                self.batch.next().map(Ok)
            }
            Ok(None) => {
                self.file = None;
                None
            }
            Err(error) => {
                self.failed = true;
                Some(Err(error))
            }
        }
    }
}
pub fn take<const N: usize>(input: &mut &[u8]) -> io::Result<[u8; N]> {
    let mut output = [0; N];
    input.read_exact(&mut output)?;
    Ok(output)
}
pub fn text(output: &mut Vec<u8>, value: &str) {
    output.extend((value.len() as u32).to_le_bytes());
    output.extend(value.as_bytes());
}
pub fn read_text(input: &mut &[u8]) -> io::Result<String> {
    let n = u32::from_le_bytes(take(input)?) as usize;
    if n > 4096 || n > input.len() {
        return Err(io::Error::other("invalid scratch text"));
    }
    let (head, tail) = input.split_at(n);
    *input = tail;
    String::from_utf8(head.to_vec()).map_err(io::Error::other)
}

impl Record for crate::results::metrics::EquityPoint {
    fn encode(&self, output: &mut Vec<u8>) {
        output.extend(self.timestamp_ns.to_le_bytes());
        output.extend(self.equity.unscaled().to_le_bytes());
    }
    fn decode(input: &mut &[u8]) -> io::Result<Self> {
        Ok(Self {
            timestamp_ns: i64::from_le_bytes(take(input)?),
            equity: num::Decimal18::from_unscaled(i128::from_le_bytes(take(input)?))
                .map_err(io::Error::other)?,
        })
    }
}
