//! Exact replay histories with bounded RAM and compressed, owned scratch files.
//!
//! This is execution scratch, not a persisted application format. All rows are
//! retained; statistics and Parquet output never require a full in-memory copy.

use std::{
    fs::{File, OpenOptions},
    io::{self, Read, Write},
    marker::PhantomData,
    sync::Arc,
};
use tempfile::NamedTempFile;

const CHUNK: usize = 8192;
const MAX_FRAME: usize = 64 * 1024 * 1024;

pub trait Record: Clone {
    fn encode(&self, output: &mut Vec<u8>);
    fn decode(input: &mut &[u8]) -> io::Result<Self>;
}

#[derive(Clone, Debug)]
pub struct History<T: Record> {
    tail: Vec<T>,
    file: Option<Arc<NamedTempFile>>,
    count: usize,
    bytes: u64,
    last: Option<T>,
}
impl<T: Record> Default for History<T> {
    fn default() -> Self {
        Self {
            tail: Vec::new(),
            file: None,
            count: 0,
            bytes: 0,
            last: None,
        }
    }
}
impl<T: Record> History<T> {
    pub fn push(&mut self, value: T) -> io::Result<()> {
        self.last = Some(value.clone());
        self.tail.push(value);
        self.count += 1;
        if self.tail.len() >= CHUNK {
            let mut raw = Vec::new();
            for record in &self.tail {
                record.encode(&mut raw);
            }
            if raw.len() > MAX_FRAME {
                return Err(io::Error::other("replay history frame exceeds capacity"));
            }
            let mut compressor = zstd::bulk::Compressor::new(1)?;
            compressor.set_parameter(zstd::zstd_safe::CParameter::ChecksumFlag(true))?;
            let compressed = compressor.compress(&raw)?;
            let cap = std::env::var("HARNESS_HISTORY_SCRATCH_BYTES")
                .ok()
                .map(|v| v.parse::<u64>().map_err(io::Error::other))
                .transpose()?
                .unwrap_or(2 * 1024 * 1024 * 1024);
            let bytes = self.bytes + compressed.len() as u64 + 16;
            if bytes > cap {
                return Err(io::Error::other(
                    "replay history scratch capacity exhausted",
                ));
            }
            if self.file.is_none() {
                self.file = Some(Arc::new(NamedTempFile::new()?));
            }
            // A completed history may be cloned by result consumers. Fork its
            // scratch before appending so each snapshot keeps its exact rows.
            if Arc::strong_count(self.file.as_ref().unwrap()) > 1 {
                let mut replacement = NamedTempFile::new()?;
                let mut original = File::open(self.file.as_ref().unwrap().path())?.take(self.bytes);
                if io::copy(&mut original, &mut replacement)? != self.bytes {
                    return Err(io::Error::other("truncated replay history"));
                }
                self.file = Some(Arc::new(replacement));
            }
            let mut file = OpenOptions::new()
                .append(true)
                .open(self.file.as_ref().unwrap().path())?;
            file.write_all(&(compressed.len() as u64).to_le_bytes())?;
            file.write_all(&(raw.len() as u64).to_le_bytes())?;
            file.write_all(&compressed)?;
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
        }
        .chain(self.tail.iter().cloned().map(Ok)))
    }
}
struct HistoryReader<T: Record> {
    file: Option<io::Take<File>>,
    batch: std::vec::IntoIter<T>,
    marker: PhantomData<T>,
    failed: bool,
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
            let mut bytes = vec![0; compressed];
            file.read_exact(&mut bytes)?;
            let raw = zstd::bulk::decompress(&bytes, size)?;
            let mut input = raw.as_slice();
            let mut records = Vec::new();
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
