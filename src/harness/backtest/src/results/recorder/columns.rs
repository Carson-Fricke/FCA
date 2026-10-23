//! Bounded typed Arrow builders; borrowed cells are copied directly into columns.
use super::schemas::Artifact;
use super::schemas::LogicalType;
use super::writer::Value;
use super::writer::WriterError;
use arrow_array::builder::BooleanBuilder;
use arrow_array::builder::StringBuilder;
use arrow_array::builder::TimestampNanosecondBuilder;
use arrow_array::builder::UInt32Builder;
use arrow_array::builder::UInt64Builder;
use arrow_array::ArrayRef;
use num::Decimal18;
use std::fmt::Write;
use std::sync::Arc;

pub(super) enum Cell<'a> {
    Text(Option<&'a str>),
    Decimal(Decimal18),
    U32(u32),
    U64(u64),
    Bool(bool),
    Time(i64),
    Null,
}
impl<'a> From<&'a Value> for Cell<'a> {
    fn from(value: &'a Value) -> Self {
        match value {
            Value::Utf8(v) => Self::Text(Some(v)),
            Value::Null => Self::Null,
            Value::UInt32(v) => Self::U32(*v),
            Value::UInt64(v) => Self::U64(*v),
            Value::Bool(v) => Self::Bool(*v),
            Value::TimestampNs(v) => Self::Time(*v),
        }
    }
}
enum Column {
    Text {
        builder: StringBuilder,
        last: Option<Decimal18>,
        text: String,
    },
    U32(UInt32Builder),
    U64(UInt64Builder),
    Bool(BooleanBuilder),
    Time(TimestampNanosecondBuilder),
}
pub(super) struct Columns {
    inner: Vec<Column>,
    pub rows: usize,
}
impl Columns {
    pub fn new(artifact: Artifact) -> Self {
        Self {
            rows: 0,
            inner: artifact
                .fields()
                .iter()
                .map(|field| match field.logical_type {
                    LogicalType::Utf8 => Column::Text {
                        builder: StringBuilder::new(),
                        last: None,
                        text: String::new(),
                    },
                    LogicalType::UInt32 => Column::U32(UInt32Builder::new()),
                    LogicalType::UInt64 => Column::U64(UInt64Builder::new()),
                    LogicalType::Bool => Column::Bool(BooleanBuilder::new()),
                    LogicalType::TimestampNsUtc => Column::Time(TimestampNanosecondBuilder::new()),
                })
                .collect(),
        }
    }
    pub fn append<'a>(
        &mut self,
        cells: impl IntoIterator<Item = Cell<'a>>,
    ) -> Result<(), WriterError> {
        for (column, cell) in self.inner.iter_mut().zip(cells) {
            match (column, cell) {
                (Column::Text { builder, last, .. }, Cell::Text(text)) => {
                    builder.append_option(text);
                    *last = None;
                }
                (
                    Column::Text {
                        builder,
                        last,
                        text,
                    },
                    Cell::Decimal(value),
                ) => {
                    if *last != Some(value) {
                        text.clear();
                        write!(text, "{value}").expect("String write");
                        *last = Some(value);
                    }
                    builder.append_value(text.as_str());
                }
                (Column::Text { builder, .. }, Cell::Null) => builder.append_null(),
                (Column::U32(b), Cell::U32(v)) => b.append_value(v),
                (Column::U64(b), Cell::U64(v)) => b.append_value(v),
                (Column::Bool(b), Cell::Bool(v)) => b.append_value(v),
                (Column::Time(b), Cell::Time(v)) => b.append_value(v),
                (Column::U32(b), Cell::Null) => b.append_null(),
                (Column::U64(b), Cell::Null) => b.append_null(),
                (Column::Bool(b), Cell::Null) => b.append_null(),
                (Column::Time(b), Cell::Null) => b.append_null(),
                _ => {
                    return Err(WriterError::Io(std::io::Error::other(
                        "typed artifact cell mismatch",
                    )))
                }
            }
        }
        self.rows += 1;
        Ok(())
    }
    pub fn finish(&mut self) -> Vec<ArrayRef> {
        self.rows = 0;
        self.inner
            .iter_mut()
            .map(|c| match c {
                Column::Text { builder, .. } => Arc::new(builder.finish()) as ArrayRef,
                Column::U32(b) => Arc::new(b.finish()),
                Column::U64(b) => Arc::new(b.finish()),
                Column::Bool(b) => Arc::new(b.finish()),
                Column::Time(b) => Arc::new(b.finish().with_timezone("UTC")),
            })
            .collect()
    }
}
