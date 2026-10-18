//! Owned scratch files with process-wide byte accounting and bounded I/O.
use lake_contracts::DataError;
use std::io::{Read, Seek, SeekFrom, Write};

#[derive(Debug)]
pub struct StagedFile {
    file: Option<tempfile::NamedTempFile>,
    sealed: Option<tempfile::TempPath>,
    reservations: Vec<lake_pipeline::Reservation>,
    size: usize,
}
impl StagedFile {
    pub fn new() -> Result<Self, DataError> {
        Ok(Self {
            file: Some(tempfile::NamedTempFile::new()?),
            sealed: None,
            reservations: Vec::new(),
            size: 0,
        })
    }
    pub fn path(&self) -> &std::path::Path {
        match &self.file {
            Some(file) => file.path(),
            None => self.sealed.as_ref().expect("sealed file").as_ref(),
        }
    }
    pub fn seal(mut self) -> Self {
        if let Some(file) = self.file.take() {
            self.sealed = Some(file.into_temp_path());
        }
        self
    }
    pub fn len(&self) -> usize {
        self.size
    }
    pub fn is_empty(&self) -> bool {
        self.size == 0
    }
    pub fn open(&self) -> Result<std::fs::File, DataError> {
        Ok(std::fs::File::open(self.path())?)
    }
    pub fn checksum(&self) -> Result<String, DataError> {
        use sha2::{Digest, Sha256};
        let mut input = self.open()?;
        input.seek(SeekFrom::Start(0))?;
        let mut digest = Sha256::new();
        let mut buffer = [0u8; 65536];
        loop {
            let n = input.read(&mut buffer)?;
            if n == 0 {
                break;
            }
            digest.update(&buffer[..n]);
        }
        Ok(hex::encode(digest.finalize()))
    }
}
impl Write for StagedFile {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        let permit = lake_pipeline::Pipeline::global()
            .scratch(bytes.len())
            .map_err(std::io::Error::other)?;
        let written = self
            .file
            .as_mut()
            .ok_or_else(|| std::io::Error::other("staged file is sealed"))?
            .write(bytes)?;
        self.size += written;
        self.reservations.push(permit);
        Ok(written)
    }
    fn flush(&mut self) -> std::io::Result<()> {
        self.file
            .as_mut()
            .ok_or_else(|| std::io::Error::other("staged file is sealed"))?
            .flush()
    }
}
