//! Bounded external merge of canonical Arrow runs; no provider or object-store policy.
use arrow_array::{Array, ArrayRef, Int64Array, RecordBatch, StringArray};
use arrow_ipc::{reader::StreamReader, writer::StreamWriter};
use arrow_row::{RowConverter, Rows, SortField};
use arrow_schema::SchemaRef;
use lake_contracts::DataError;
use lake_storage::StagedFile;
use std::sync::Arc;

const FAN_IN: usize = 16;
const BATCH_ROWS: usize = 16_384;

pub(crate) struct SortedRuns {
    schema: SchemaRef,
    files: Vec<Arc<StagedFile>>,
}
impl SortedRuns {
    pub fn new(schema: SchemaRef) -> Self {
        Self {
            schema,
            files: Vec::new(),
        }
    }
    pub fn push(&mut self, batch: RecordBatch) -> Result<(), DataError> {
        if batch.num_rows() > 0 {
            self.files
                .push(write_run(self.schema.clone(), std::iter::once(Ok(batch)))?);
        }
        Ok(())
    }
    pub fn finish(mut self) -> Result<MergeBatches, DataError> {
        while self.files.len() > FAN_IN {
            let mut next = Vec::new();
            let mut files = self.files.into_iter();
            loop {
                let group: Vec<_> = files.by_ref().take(FAN_IN).collect();
                if group.is_empty() {
                    break;
                }
                next.push(write_run(
                    self.schema.clone(),
                    MergeBatches::new(self.schema.clone(), group)?,
                )?);
            }
            self.files = next;
        }
        MergeBatches::new(self.schema, self.files)
    }
}
fn write_run(
    schema: SchemaRef,
    batches: impl Iterator<Item = Result<RecordBatch, DataError>>,
) -> Result<Arc<StagedFile>, DataError> {
    let mut spill = lake_pipeline::Pipeline::global().timer(lake_pipeline::Stage::Spill);
    let mut file = StagedFile::new()?;
    {
        let mut writer = StreamWriter::try_new(&mut file, &schema).map_err(DataError::io)?;
        for batch in batches {
            lake_pipeline::Pipeline::global().check()?;
            writer.write(&batch?).map_err(DataError::io)?;
        }
        writer.finish().map_err(DataError::io)?;
    }
    spill.bytes(file.len());
    Ok(Arc::new(file.seal()))
}

struct Cursor {
    _file: Arc<StagedFile>,
    reader: StreamReader<std::fs::File>,
    batch: RecordBatch,
    keys: Rows,
    index: usize,
    source: Option<usize>,
}

/// At most FAN_IN input batches plus one bounded output selection are retained.
pub(crate) struct MergeBatches {
    schema: SchemaRef,
    converter: RowConverter,
    cursors: Vec<Cursor>,
    heap: Vec<usize>,
    finished: bool,
}
impl MergeBatches {
    fn new(schema: SchemaRef, files: Vec<Arc<StagedFile>>) -> Result<Self, DataError> {
        let _sort = lake_pipeline::Pipeline::global().timer(lake_pipeline::Stage::Sort);
        let fields = schema
            .fields()
            .iter()
            .filter(|f| f.name() != "symbol")
            .map(|f| SortField::new(f.data_type().clone()))
            .collect();
        let converter = RowConverter::new(fields).map_err(DataError::io)?;
        let mut cursors = Vec::new();
        for file in files {
            let mut reader = StreamReader::try_new(file.open()?, None).map_err(DataError::io)?;
            if let Some(batch) = reader.next() {
                let batch = batch.map_err(DataError::io)?;
                let keys = converter
                    .convert_columns(&key_columns(&batch))
                    .map_err(DataError::io)?;
                if batch.num_rows() > 0 {
                    cursors.push(Cursor {
                        _file: file,
                        reader,
                        batch,
                        keys,
                        index: 0,
                        source: None,
                    });
                }
            }
        }
        let mut result = Self {
            schema,
            converter,
            cursors,
            heap: Vec::new(),
            finished: false,
        };
        for index in 0..result.cursors.len() {
            result.heap.push(index);
            let mut pos = result.heap.len() - 1;
            while pos > 0 {
                let parent = (pos - 1) / 2;
                if !result.less(pos, parent) {
                    break;
                }
                result.heap.swap(pos, parent);
                pos = parent;
            }
        }
        Ok(result)
    }
    fn less(&self, left: usize, right: usize) -> bool {
        let a = &self.cursors[self.heap[left]];
        let b = &self.cursors[self.heap[right]];
        a.keys.row(a.index) < b.keys.row(b.index)
    }
    fn repair(&mut self) {
        let mut pos = 0;
        loop {
            let a = 2 * pos + 1;
            if a >= self.heap.len() {
                break;
            }
            let b = a + 1;
            let child = if b < self.heap.len() && self.less(b, a) {
                b
            } else {
                a
            };
            if !self.less(child, pos) {
                break;
            }
            self.heap.swap(pos, child);
            pos = child;
        }
    }
    fn batch(&mut self) -> Result<Option<RecordBatch>, DataError> {
        if self.finished {
            return Ok(None);
        }
        if self.heap.is_empty() {
            self.finished = true;
            return Ok(Some(RecordBatch::new_empty(self.schema.clone())));
        }
        let _sort = lake_pipeline::Pipeline::global().timer(lake_pipeline::Stage::Sort);
        // Emit fixed-size canonical batches independent of page/arrival boundaries.
        // Disjoint runs are consumed in bulk, avoiding per-row heap comparisons.
        let mut sources = Vec::new();
        let mut selection = Vec::with_capacity(BATCH_ROWS);
        while !self.heap.is_empty() && selection.len() < BATCH_ROWS {
            let index = self.heap[0];
            let next = match self.heap.len() {
                1 => None,
                2 => Some(1),
                _ => Some(if self.less(1, 2) { 1 } else { 2 }),
            };
            let bulk = next.is_none_or(|next| {
                let current = &self.cursors[index];
                let other = &self.cursors[self.heap[next]];
                current.keys.row(current.batch.num_rows() - 1) <= other.keys.row(other.index)
            });
            let cursor = &mut self.cursors[index];
            let source = *cursor.source.get_or_insert_with(|| {
                let source = sources.len();
                sources.push(cursor.batch.clone());
                source
            });
            let take = if bulk {
                (cursor.batch.num_rows() - cursor.index).min(BATCH_ROWS - selection.len())
            } else {
                1
            };
            selection.extend((cursor.index..cursor.index + take).map(|row| (source, row)));
            cursor.index += take;
            if cursor.index == cursor.batch.num_rows() {
                match cursor.reader.next() {
                    Some(batch) => {
                        cursor.batch = batch.map_err(DataError::io)?;
                        cursor.keys = self
                            .converter
                            .convert_columns(&key_columns(&cursor.batch))
                            .map_err(DataError::io)?;
                        cursor.index = 0;
                        cursor.source = None;
                    }
                    None => {
                        self.heap.swap_remove(0);
                    }
                }
            }
            self.repair();
        }
        for cursor in &mut self.cursors {
            cursor.source = None;
        }
        self.finished = self.heap.is_empty();
        let sources: Vec<_> = sources.iter().collect();
        arrow_select::interleave::interleave_record_batch(&sources, &selection)
            .map(Some)
            .map_err(DataError::io)
    }
}
impl Iterator for MergeBatches {
    type Item = Result<RecordBatch, DataError>;
    fn next(&mut self) -> Option<Self::Item> {
        match self.batch() {
            Ok(Some(batch)) => Some(Ok(batch)),
            Ok(None) => None,
            Err(error) => {
                self.finished = true;
                Some(Err(error))
            }
        }
    }
}
/// Canonical comparators treat optional text as empty and crypto trade IDs as -1.
fn key_columns(batch: &RecordBatch) -> Vec<ArrayRef> {
    batch
        .schema()
        .fields()
        .iter()
        .zip(batch.columns())
        .filter(|(field, _)| field.name() != "symbol")
        .map(|(_, array)| {
            if array.null_count() == 0 {
                return array.clone();
            }
            if let Some(values) = array.as_any().downcast_ref::<StringArray>() {
                return Arc::new(StringArray::from_iter_values(
                    values.iter().map(|v| v.unwrap_or("")),
                )) as ArrayRef;
            }
            if let Some(values) = array.as_any().downcast_ref::<Int64Array>() {
                return Arc::new(Int64Array::from_iter_values(
                    values.iter().map(|v| v.unwrap_or(-1)),
                )) as ArrayRef;
            }
            array.clone()
        })
        .collect()
}
