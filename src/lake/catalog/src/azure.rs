//! Azure Table mappings and adapters for the catalog ports.
//!
//! Ports `lake.catalog.azure`: entity mappings that keep the exact row/key
//! and percent-encoding rules (including the string-exact decimal and metrics
//! columns), the object-safe [`TableEntityStore`] seam the repositories are
//! built over, and the real `azure_data_tables` implementation. Hermetic
//! tests inject a fake store exactly like the Python suite duck-typed
//! `TableClient`.

use std::collections::BTreeMap;
use std::sync::Arc;

use lake_contracts::{
    canonical_symbol, AssetClass, DataError, DataType, Date, DateTime, Decimal18,
};
use serde_json::Value;

use crate::models::{
    table_key, BarSource, ContractRecord, CoverageRecord, CoverageStatus, DelistingType, JobRecord,
    JobState, ListingRecord, ListingType, OptionType, RunRecord, RunState,
};
use crate::ports::{
    CatalogRepository, ContractRepository, CoverageQuery, JobRepository, ListingRepository,
    RunRepository,
};

const COVERAGE_PARTITION_PREFIX: &str = "coverage";
const JOB_PARTITION_KEY: &str = "jobs";
const RUN_PARTITION_KEY: &str = "runs";
const CONTRACT_PARTITION_KEY: &str = "contracts";
const LISTING_PARTITION_PREFIX: &str = "listings";

/// One Azure Table entity as a JSON object.
pub type TableEntity = serde_json::Map<String, Value>;

/// A named-parameter OData query, mirroring the Python `query_entities` call.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TableQuery {
    /// The OData filter with `@name` placeholders.
    pub filter: String,
    /// Named parameter bindings, like Python's `parameters` mapping.
    pub parameters: BTreeMap<String, String>,
}

impl TableQuery {
    /// The exact-partition query the repositories use for qualified scans.
    pub fn partition_eq(partition: &str) -> Self {
        let mut parameters = BTreeMap::new();
        parameters.insert("partition".to_string(), partition.to_string());
        Self {
            filter: "PartitionKey eq @partition".to_string(),
            parameters,
        }
    }

    /// The partition-range query used for unqualified scans.
    pub fn partition_range(start: &str, end: &str) -> Self {
        let mut parameters = BTreeMap::new();
        parameters.insert("start".to_string(), start.to_string());
        parameters.insert("end".to_string(), end.to_string());
        Self {
            filter: "PartitionKey ge @start and PartitionKey lt @end".to_string(),
            parameters,
        }
    }
}

/// The table surface the catalog repositories are built over.
///
/// Mirrors the `TableClient` operations the Python adapters duck-type:
/// point reads, replace-mode upserts, filtered queries, and deletes.
pub trait TableEntityStore: Send + Sync {
    /// Return one entity or [`DataError::NotFound`].
    fn get_entity(&self, partition_key: &str, row_key: &str) -> Result<TableEntity, DataError>;

    /// Upsert one entity with replace semantics.
    fn upsert_entity(&self, entity: TableEntity) -> Result<(), DataError>;

    /// Run one parameterized filter query.
    fn query_entities(&self, query: &TableQuery) -> Result<Vec<TableEntity>, DataError>;

    /// Delete one entity; missing entities succeed silently.
    fn delete_entity(&self, partition_key: &str, row_key: &str) -> Result<(), DataError>;
}

/// Read one required string column, mirroring `str(entity[name])`.
fn entity_string(entity: &TableEntity, name: &str) -> Result<String, DataError> {
    match entity.get(name) {
        Some(Value::String(text)) => Ok(text.clone()),
        Some(_) | None => Err(DataError::type_error(format!(
            "{name} must be a string value"
        ))),
    }
}

/// Read one optional string column, mirroring `_optional_string`.
fn optional_string(entity: &TableEntity, name: &str) -> Option<String> {
    match entity.get(name) {
        None | Some(Value::Null) => None,
        Some(Value::String(text)) => Some(text.clone()),
        Some(other) => Some(other.to_string()),
    }
}

/// Read one optional non-negative integer column, mirroring `_integer`.
fn entity_integer(entity: &TableEntity, name: &str, default: u64) -> Result<u64, DataError> {
    match entity.get(name) {
        None | Some(Value::Null) => Ok(default),
        Some(Value::Number(number)) => number
            .as_u64()
            .ok_or_else(|| DataError::value(format!("{name} must be an integer"))),
        Some(Value::String(text)) => text
            .parse::<u64>()
            .map_err(|_| DataError::value(format!("{name} must be an integer"))),
        Some(_) => Err(DataError::value(format!("{name} must be an integer"))),
    }
}

/// Parse one optional `YYYY-MM-DD` column, matching Python's
/// `date.fromisoformat` wording on failure.
fn optional_date(entity: &TableEntity, name: &str) -> Result<Option<Date>, DataError> {
    match optional_string(entity, name) {
        None => Ok(None),
        Some(text) => Date::parse(&text)
            .map(Some)
            .ok_or_else(|| DataError::value(format!("Invalid isoformat string: {text:?}"))),
    }
}

/// Parse one required `YYYY-MM-DD` column, matching Python's
/// `date.fromisoformat` wording on failure.
fn required_date(entity: &TableEntity, name: &str) -> Result<Date, DataError> {
    let text = entity_string(entity, name)?;
    Date::parse(&text)
        .ok_or_else(|| DataError::value(format!("Invalid isoformat string: {text:?}")))
}

/// Parse one required persisted enum column.
fn enum_value<T: Copy>(
    entity: &TableEntity,
    name: &str,
    type_name: &str,
    parse: fn(&str) -> Option<T>,
) -> Result<T, DataError> {
    parse_enum(&entity_string(entity, name)?, type_name, parse)
}

/// Parse one persisted enum value with the models' error wording.
fn parse_enum<T: Copy>(
    text: &str,
    type_name: &str,
    parse: fn(&str) -> Option<T>,
) -> Result<T, DataError> {
    parse(text).ok_or_else(|| DataError::value(format!("{text:?} is not a valid {type_name}")))
}

/// The Azure partition key shared by memory and Table coverage adapters.
pub fn coverage_partition_key(record: &CoverageRecord) -> String {
    record.partition_key()
}

/// The Azure partition key for one canonical entity identifier.
pub fn listing_partition_for(entity_id: &str) -> String {
    format!(
        "{LISTING_PARTITION_PREFIX}|{}",
        table_key(entity_id.trim().to_uppercase().as_str())
    )
}

/// Map coverage metadata to an Azure Table-compatible entity.
pub fn coverage_to_entity(record: &CoverageRecord) -> TableEntity {
    let mut entity = TableEntity::new();
    entity.insert(
        "PartitionKey".to_string(),
        Value::String(record.partition_key()),
    );
    entity.insert("RowKey".to_string(), Value::String(record.row_key()));
    entity.insert(
        "AssetClass".to_string(),
        Value::String(record.asset_class.value().to_string()),
    );
    entity.insert(
        "DataType".to_string(),
        Value::String(record.data_type.value().to_string()),
    );
    entity.insert("Symbol".to_string(), Value::String(record.symbol.clone()));
    entity.insert(
        "SessionDate".to_string(),
        Value::String(record.session_date.isoformat()),
    );
    entity.insert(
        "Status".to_string(),
        Value::String(record.status.value().to_string()),
    );
    entity.insert("RowCount".to_string(), Value::from(record.row_count));
    entity.insert("ByteSize".to_string(), Value::from(record.byte_size));
    if let Some(object_path) = &record.object_path {
        entity.insert("ObjectPath".to_string(), Value::String(object_path.clone()));
    }
    if let Some(checksum) = &record.checksum {
        entity.insert("Checksum".to_string(), Value::String(checksum.clone()));
    }
    if let Some(error_message) = &record.error_message {
        entity.insert(
            "ErrorMessage".to_string(),
            Value::String(error_message.clone()),
        );
    }
    if let Some(interval) = &record.interval {
        entity.insert("Interval".to_string(), Value::String(interval.clone()));
    }
    if let Some(bar_source) = &record.bar_source {
        entity.insert(
            "BarSource".to_string(),
            Value::String(bar_source.value().to_string()),
        );
    }
    if let Some(source_revision) = &record.source_revision {
        entity.insert(
            "SourceRevision".to_string(),
            Value::String(source_revision.clone()),
        );
    }
    entity
}

/// Restore a coverage record from an Azure Table entity.
pub fn coverage_from_entity(entity: &TableEntity) -> Result<CoverageRecord, DataError> {
    let session_date = entity_string(entity, "SessionDate")?;
    let session_date = Date::parse(&session_date).ok_or_else(|| {
        DataError::value(format!(
            "Invalid isoformat string: {:?}",
            entity_string(entity, "SessionDate").unwrap_or_default()
        ))
    })?;
    let asset_class =
        enum_value::<AssetClass>(entity, "AssetClass", "AssetClass", AssetClass::from_value)?;
    let data_type = enum_value::<DataType>(entity, "DataType", "DataType", DataType::from_value)?;
    let status = enum_value::<CoverageStatus>(
        entity,
        "Status",
        "CoverageStatus",
        CoverageStatus::from_value,
    )?;
    let record = CoverageRecord::new(
        asset_class,
        data_type,
        entity_string(entity, "Symbol")?,
        session_date,
        status,
        optional_string(entity, "ObjectPath"),
        entity_integer(entity, "RowCount", 0)?,
        entity_integer(entity, "ByteSize", 0)?,
        optional_string(entity, "Checksum"),
        optional_string(entity, "ErrorMessage"),
        optional_string(entity, "Interval"),
    )?;
    match optional_string(entity, "BarSource") {
        Some(raw) => {
            let source = BarSource::from_value(&raw)
                .ok_or_else(|| DataError::value(format!("{raw:?} is not a valid BarSource")))?;
            let revision = optional_string(entity, "SourceRevision")
                .ok_or_else(|| DataError::value("coverage BarSource requires a SourceRevision"))?;
            record.with_bar_provenance(source, revision)
        }
        None => {
            if optional_string(entity, "SourceRevision").is_some() {
                return Err(DataError::value(
                    "coverage SourceRevision requires a BarSource",
                ));
            }
            Ok(record)
        }
    }
}

/// Map job state and UTC timestamps to an Azure Table-compatible entity.
pub fn job_to_entity(record: &JobRecord) -> TableEntity {
    let mut entity = TableEntity::new();
    entity.insert("PartitionKey".to_string(), Value::from(JOB_PARTITION_KEY));
    entity.insert("RowKey".to_string(), Value::String(record.row_key()));
    entity.insert("JobId".to_string(), Value::String(record.job_id.clone()));
    entity.insert(
        "State".to_string(),
        Value::String(record.state.value().to_string()),
    );
    entity.insert(
        "CreatedAt".to_string(),
        Value::String(record.created_at.isoformat_micros_utc()),
    );
    entity.insert(
        "CreatedAt@odata.type".to_string(),
        Value::from("Edm.DateTime"),
    );
    entity.insert(
        "UpdatedAt".to_string(),
        Value::String(record.updated_at.isoformat_micros_utc()),
    );
    entity.insert(
        "UpdatedAt@odata.type".to_string(),
        Value::from("Edm.DateTime"),
    );
    if let Some(error_message) = &record.error_message {
        entity.insert(
            "ErrorMessage".to_string(),
            Value::String(error_message.clone()),
        );
    }
    entity
}

/// Read one job timestamp column, requiring a datetime-shaped string.
fn job_timestamp(entity: &TableEntity, name: &str) -> Result<DateTime, DataError> {
    let parse = |value: &Value| match value {
        Value::String(text) => DateTime::parse_iso_utc(text)
            .ok_or_else(|| DataError::type_error("job entity timestamps must be datetime values")),
        _ => Err(DataError::type_error(
            "job entity timestamps must be datetime values",
        )),
    };
    entity.get(name).map_or_else(
        || {
            Err(DataError::type_error(
                "job entity timestamps must be datetime values",
            ))
        },
        parse,
    )
}

/// Restore a job record from an Azure Table entity.
pub fn job_from_entity(entity: &TableEntity) -> Result<JobRecord, DataError> {
    let created_at = job_timestamp(entity, "CreatedAt")?;
    let updated_at = job_timestamp(entity, "UpdatedAt")?;
    JobRecord::new(
        entity_string(entity, "JobId")?,
        enum_value::<JobState>(entity, "State", "JobState", JobState::from_value)?,
        created_at,
        updated_at,
        optional_string(entity, "ErrorMessage"),
    )
}

/// Serialize metric values into one canonical, string-exact JSON column.
fn metrics_json(metrics: &BTreeMap<String, String>) -> String {
    let text = serde_json::to_string(metrics).expect("metrics map serializes");
    ensure_ascii(&text)
}

/// Escape non-ASCII like Python's `json.dumps(ensure_ascii=True)`.
fn ensure_ascii(text: &str) -> String {
    let mut escaped = String::with_capacity(text.len());
    for unit in text.encode_utf16() {
        if unit < 0x80 {
            escaped.push(unit as u8 as char);
        } else {
            escaped.push_str(&format!("\\u{unit:04X}"));
        }
    }
    escaped
}

/// Map run metadata and canonical metrics JSON to a Table entity.
pub fn run_to_entity(record: &RunRecord) -> TableEntity {
    let mut entity = TableEntity::new();
    entity.insert("PartitionKey".to_string(), Value::from(RUN_PARTITION_KEY));
    entity.insert("RowKey".to_string(), Value::String(record.row_key()));
    entity.insert("JobId".to_string(), Value::String(record.job_id.clone()));
    entity.insert(
        "Status".to_string(),
        Value::String(record.status.value().to_string()),
    );
    entity.insert(
        "StrategyId".to_string(),
        Value::String(record.strategy_id.clone()),
    );
    entity.insert(
        "ArtifactCount".to_string(),
        Value::from(record.artifact_count),
    );
    entity.insert("TotalBytes".to_string(), Value::from(record.total_bytes));
    entity.insert(
        "Metrics".to_string(),
        Value::String(metrics_json(&record.metrics)),
    );
    if let Some(data_start) = record.data_start {
        entity.insert(
            "DataStart".to_string(),
            Value::String(data_start.isoformat()),
        );
    }
    if let Some(data_end) = record.data_end {
        entity.insert("DataEnd".to_string(), Value::String(data_end.isoformat()));
    }
    if let Some(run_digest) = &record.run_digest {
        entity.insert("RunDigest".to_string(), Value::String(run_digest.clone()));
    }
    if let Some(configuration_digest) = &record.configuration_digest {
        entity.insert(
            "ConfigurationDigest".to_string(),
            Value::String(configuration_digest.clone()),
        );
    }
    entity
}

/// Restore a run record from an Azure Table entity.
pub fn run_from_entity(entity: &TableEntity) -> Result<RunRecord, DataError> {
    let data_start = optional_date(entity, "DataStart")?;
    let data_end = optional_date(entity, "DataEnd")?;
    let metrics_text = optional_string(entity, "Metrics").unwrap_or_else(|| "{}".to_string());
    let decoded: Value = serde_json::from_str(&metrics_text)
        .map_err(|_| DataError::type_error("run entity metrics must decode to a JSON object"))?;
    let object = decoded
        .as_object()
        .ok_or_else(|| DataError::type_error("run entity metrics must decode to a JSON object"))?;
    let mut metrics = BTreeMap::new();
    for (key, value) in object {
        let text = value
            .as_str()
            .ok_or_else(|| DataError::type_error("run entity metrics must be a string mapping"))?;
        metrics.insert(key.clone(), text.to_string());
    }
    RunRecord::new(
        entity_string(entity, "JobId")?,
        enum_value::<RunState>(entity, "Status", "RunState", RunState::from_value)?,
        entity_string(entity, "StrategyId")?,
        data_start,
        data_end,
        optional_string(entity, "RunDigest"),
        optional_string(entity, "ConfigurationDigest"),
        entity_integer(entity, "ArtifactCount", 0)?,
        entity_integer(entity, "TotalBytes", 0)?,
        metrics,
    )
}

/// Map contract metadata to an Azure Table-compatible entity.
pub fn contract_to_entity(record: &ContractRecord) -> TableEntity {
    let mut entity = TableEntity::new();
    entity.insert(
        "PartitionKey".to_string(),
        Value::from(CONTRACT_PARTITION_KEY),
    );
    entity.insert(
        "RowKey".to_string(),
        Value::String(table_key(&record.symbol)),
    );
    entity.insert("Symbol".to_string(), Value::String(record.symbol.clone()));
    entity.insert(
        "Underlying".to_string(),
        Value::String(record.underlying.clone()),
    );
    entity.insert(
        "ExpirationDate".to_string(),
        Value::String(record.expiration_date.isoformat()),
    );
    // Azure Tables have no decimal EDM type; strings preserve the exact
    // strike value instead of silently converting it to a float.
    entity.insert(
        "StrikePrice".to_string(),
        Value::String(record.strike_price.to_string()),
    );
    entity.insert(
        "OptionType".to_string(),
        Value::String(record.option_type.value().to_string()),
    );
    entity.insert("Multiplier".to_string(), Value::from(record.multiplier));
    if let Some(listed_date) = record.listed_date {
        entity.insert(
            "ListedDate".to_string(),
            Value::String(listed_date.isoformat()),
        );
    }
    if let Some(delisted_date) = record.delisted_date {
        entity.insert(
            "DelistedDate".to_string(),
            Value::String(delisted_date.isoformat()),
        );
    }
    entity
}

/// Restore a contract record from an Azure Table entity.
pub fn contract_from_entity(entity: &TableEntity) -> Result<ContractRecord, DataError> {
    let expiration_text = entity_string(entity, "ExpirationDate")?;
    let expiration_date = Date::parse(&expiration_text).ok_or_else(|| {
        DataError::value(format!("Invalid isoformat string: {expiration_text:?}"))
    })?;
    let strike_text = entity_string(entity, "StrikePrice")?;
    let strike_price =
        Decimal18::parse(&strike_text).map_err(|error| DataError::value(error.to_string()))?;
    let listed_date = optional_date(entity, "ListedDate")?;
    let delisted_date = optional_date(entity, "DelistedDate")?;
    ContractRecord::new(
        entity_string(entity, "Symbol")?,
        entity_string(entity, "Underlying")?,
        expiration_date,
        strike_price,
        enum_value::<OptionType>(entity, "OptionType", "OptionType", OptionType::from_value)?,
        u32::try_from(entity_integer(entity, "Multiplier", 100)?)
            .map_err(|_| DataError::value("multiplier must be a non-negative integer"))?,
        listed_date,
        delisted_date,
    )
}

/// Build the Azure partition key shared by memory and Table listing adapters.
pub fn listing_partition_key(record: &ListingRecord) -> String {
    listing_partition_for(&record.entity_id)
}

/// Map a listing episode to an Azure Table-compatible entity.
pub fn listing_to_entity(record: &ListingRecord) -> TableEntity {
    let mut entity = TableEntity::new();
    entity.insert(
        "PartitionKey".to_string(),
        Value::String(record.partition_key()),
    );
    entity.insert("RowKey".to_string(), Value::String(record.row_key()));
    entity.insert(
        "EntityId".to_string(),
        Value::String(record.entity_id.clone()),
    );
    entity.insert("Symbol".to_string(), Value::String(record.symbol.clone()));
    entity.insert(
        "ListedDate".to_string(),
        Value::String(record.listed_date.isoformat()),
    );
    entity.insert(
        "ListingType".to_string(),
        Value::String(record.listing_type.value().to_string()),
    );
    if let Some(delisted_date) = record.delisted_date {
        entity.insert(
            "DelistedDate".to_string(),
            Value::String(delisted_date.isoformat()),
        );
    }
    if let Some(delisting_type) = record.delisting_type {
        entity.insert(
            "DelistingType".to_string(),
            Value::String(delisting_type.value().to_string()),
        );
    }
    if let Some(final_price) = record.final_price {
        // Azure Tables have no decimal EDM type; strings preserve the exact
        // price value instead of silently converting it to a float.
        entity.insert(
            "FinalPrice".to_string(),
            Value::String(final_price.to_string()),
        );
    }
    entity
}

/// Restore a listing episode from an Azure Table entity.
pub fn listing_from_entity(entity: &TableEntity) -> Result<ListingRecord, DataError> {
    let delisting_type = match optional_string(entity, "DelistingType") {
        Some(text) => Some(parse_enum(
            &text,
            "DelistingType",
            DelistingType::from_value,
        )?),
        None => None,
    };
    let final_price = match optional_string(entity, "FinalPrice") {
        Some(text) => {
            Some(Decimal18::parse(&text).map_err(|error| DataError::value(error.to_string()))?)
        }
        None => None,
    };
    ListingRecord::new(
        entity_string(entity, "EntityId")?,
        entity_string(entity, "Symbol")?,
        required_date(entity, "ListedDate")?,
        optional_date(entity, "DelistedDate")?,
        enum_value::<ListingType>(
            entity,
            "ListingType",
            "ListingType",
            ListingType::from_value,
        )?,
        delisting_type,
        final_price,
    )
}

/// Whether a contract is active relative to an optional as-of date.
///
/// Mirrors `_is_active`: without an as-of date every contract is active, and
/// otherwise the day must fall inside the known listing window.
fn is_active(record: &ContractRecord, as_of: Option<Date>) -> bool {
    match as_of {
        None => true,
        Some(day) => {
            record.listed_date.is_none_or(|listed| day >= listed)
                && record.delisted_date.is_none_or(|delisted| day <= delisted)
        }
    }
}

/// The coverage partition chosen for one filter combination.
///
/// Mirrors `_coverage_partition_for_filters`: with all three filters present
/// the scan targets the exact partition; otherwise it covers the prefixed
/// partitions with a range query. Bars rows append their interval token so
/// distinct intervals never share a partition.
fn coverage_partition_for_filters(
    asset_class: Option<AssetClass>,
    data_type: Option<DataType>,
    symbol: Option<&str>,
    interval: Option<&str>,
) -> Result<String, DataError> {
    match (asset_class, data_type, symbol) {
        (Some(asset_class), Some(data_type), Some(symbol)) => {
            let symbol = canonical_symbol(symbol)?;
            let base = format!(
                "{COVERAGE_PARTITION_PREFIX}|{}|{}|{}",
                asset_class.value(),
                data_type.value(),
                table_key(&symbol)
            );
            Ok(match interval {
                Some(interval) => format!("{base}|interval={}", table_key(interval)),
                None => base,
            })
        }
        _ => Ok(COVERAGE_PARTITION_PREFIX.to_string()),
    }
}

/// The coverage query mirroring `_coverage_query`.
///
/// A bars scan only targets an exact partition when the interval filter is
/// present; without it the prefixed range query is used.
fn coverage_query(
    asset_class: Option<AssetClass>,
    data_type: Option<DataType>,
    symbol: Option<&str>,
    interval: Option<&str>,
) -> Result<TableQuery, DataError> {
    let exact = asset_class.is_some()
        && data_type.is_some()
        && symbol.is_some()
        && (data_type != Some(DataType::Bars) || interval.is_some());
    if exact {
        return Ok(TableQuery::partition_eq(&coverage_partition_for_filters(
            asset_class,
            data_type,
            symbol,
            interval,
        )?));
    }
    Ok(TableQuery::partition_range("coverage|", "coverage}"))
}

/// Catalog repository backed by an Azure Tables store.
pub struct AzureTableCatalogRepository {
    table: Arc<dyn TableEntityStore>,
}

impl AzureTableCatalogRepository {
    /// Build the repository over an injected table store.
    pub fn new(table: Arc<dyn TableEntityStore>) -> Self {
        Self { table }
    }
}

impl CatalogRepository for AzureTableCatalogRepository {
    fn get(
        &self,
        asset_class: AssetClass,
        data_type: DataType,
        interval: Option<&str>,
        symbol: &str,
        session_date: Date,
    ) -> Result<Option<CoverageRecord>, DataError> {
        let partition = coverage_partition_for_filters(
            Some(asset_class),
            Some(data_type),
            Some(symbol),
            interval,
        )?;
        let row = match interval {
            Some(interval) => format!(
                "{}|interval={}",
                session_date.isoformat(),
                table_key(interval)
            ),
            None => session_date.isoformat(),
        };
        match self.table.get_entity(&partition, &row) {
            Ok(entity) => Ok(Some(coverage_from_entity(&entity)?)),
            Err(DataError::NotFound(_)) => Ok(None),
            Err(error) => Err(error),
        }
    }

    fn upsert(&self, record: &CoverageRecord) -> Result<(), DataError> {
        self.table.upsert_entity(coverage_to_entity(record))
    }

    fn list(&self, query: &CoverageQuery) -> Result<Vec<CoverageRecord>, DataError> {
        let CoverageQuery {
            asset_class,
            data_type,
            symbol,
            interval,
            start,
            end,
        } = query;
        if let (Some(start), Some(end)) = (*start, *end) {
            if start > end {
                return Err(DataError::value("start must be on or before end"));
            }
        }
        let table_query = coverage_query(
            *asset_class,
            *data_type,
            symbol.as_deref(),
            interval.as_deref(),
        )?;
        let entities = self.table.query_entities(&table_query)?;
        let mut records = entities
            .iter()
            .map(coverage_from_entity)
            .collect::<Result<Vec<_>, _>>()?;
        records.sort_by(|left, right| left.sort_key().cmp(&right.sort_key()));
        let canonical = symbol.as_deref().map(canonical_symbol).transpose()?;
        records.retain(|record| {
            asset_class.is_none_or(|filter| record.asset_class == filter)
                && data_type.is_none_or(|filter| record.data_type == filter)
                && canonical
                    .as_deref()
                    .is_none_or(|filter| record.symbol == filter)
                && interval
                    .as_deref()
                    .is_none_or(|filter| record.interval.as_deref() == Some(filter))
                && start.is_none_or(|filter| record.session_date >= filter)
                && end.is_none_or(|filter| record.session_date <= filter)
        });
        Ok(records)
    }
}

/// Job repository backed by an Azure Tables store.
pub struct AzureTableJobRepository {
    table: Arc<dyn TableEntityStore>,
}

impl AzureTableJobRepository {
    /// Build the repository over an injected table store.
    pub fn new(table: Arc<dyn TableEntityStore>) -> Self {
        Self { table }
    }
}

impl JobRepository for AzureTableJobRepository {
    fn get(&self, job_id: &str) -> Result<Option<JobRecord>, DataError> {
        let job_id = job_id.trim();
        match self.table.get_entity(JOB_PARTITION_KEY, &table_key(job_id)) {
            Ok(entity) => Ok(Some(job_from_entity(&entity)?)),
            Err(DataError::NotFound(_)) => Ok(None),
            Err(error) => Err(error),
        }
    }

    fn upsert(&self, record: &JobRecord) -> Result<(), DataError> {
        self.table.upsert_entity(job_to_entity(record))
    }

    fn list(&self, state: Option<JobState>) -> Result<Vec<JobRecord>, DataError> {
        let entities = self
            .table
            .query_entities(&TableQuery::partition_eq(JOB_PARTITION_KEY))?;
        let mut records = entities
            .iter()
            .map(job_from_entity)
            .collect::<Result<Vec<_>, _>>()?;
        records.sort_by(|left, right| left.job_id.cmp(&right.job_id));
        records.retain(|record| state.is_none_or(|filter| record.state == filter));
        Ok(records)
    }
}

/// Run repository backed by an Azure Tables store.
pub struct AzureTableRunRepository {
    table: Arc<dyn TableEntityStore>,
}

impl AzureTableRunRepository {
    /// Build the repository over an injected table store.
    pub fn new(table: Arc<dyn TableEntityStore>) -> Self {
        Self { table }
    }
}

impl RunRepository for AzureTableRunRepository {
    fn get(&self, job_id: &str) -> Result<Option<RunRecord>, DataError> {
        let job_id = job_id.trim();
        match self.table.get_entity(RUN_PARTITION_KEY, &table_key(job_id)) {
            Ok(entity) => Ok(Some(run_from_entity(&entity)?)),
            Err(DataError::NotFound(_)) => Ok(None),
            Err(error) => Err(error),
        }
    }

    fn upsert(&self, record: &RunRecord) -> Result<(), DataError> {
        self.table.upsert_entity(run_to_entity(record))
    }

    fn list(
        &self,
        status: Option<RunState>,
        strategy_id: Option<&str>,
    ) -> Result<Vec<RunRecord>, DataError> {
        let entities = self
            .table
            .query_entities(&TableQuery::partition_eq(RUN_PARTITION_KEY))?;
        let mut records = entities
            .iter()
            .map(run_from_entity)
            .collect::<Result<Vec<_>, _>>()?;
        records.sort_by(|left, right| left.job_id.cmp(&right.job_id));
        let strategy_id = strategy_id.map(str::trim);
        records.retain(|record| {
            status.is_none_or(|filter| record.status == filter)
                && strategy_id.is_none_or(|filter| record.strategy_id == filter)
        });
        Ok(records)
    }

    fn delete(&self, job_id: &str) -> Result<bool, DataError> {
        let job_id = job_id.trim();
        let row_key = table_key(job_id);
        match self.table.get_entity(RUN_PARTITION_KEY, &row_key) {
            Ok(_) => {}
            Err(DataError::NotFound(_)) => return Ok(false),
            Err(error) => return Err(error),
        }
        // The SDK's delete succeeds silently for missing entities, so
        // existence is confirmed by the lookup above before the delete call.
        self.table.delete_entity(RUN_PARTITION_KEY, &row_key)?;
        Ok(true)
    }
}

/// Contract repository backed by an Azure Tables store.
pub struct AzureTableContractRepository {
    table: Arc<dyn TableEntityStore>,
}

impl AzureTableContractRepository {
    /// Build the repository over an injected table store.
    pub fn new(table: Arc<dyn TableEntityStore>) -> Self {
        Self { table }
    }
}

impl ContractRepository for AzureTableContractRepository {
    fn get(&self, symbol: &str) -> Result<Option<ContractRecord>, DataError> {
        let symbol = canonical_symbol(symbol)?;
        match self
            .table
            .get_entity(CONTRACT_PARTITION_KEY, &table_key(&symbol))
        {
            Ok(entity) => Ok(Some(contract_from_entity(&entity)?)),
            Err(DataError::NotFound(_)) => Ok(None),
            Err(error) => Err(error),
        }
    }

    fn upsert(&self, record: &ContractRecord) -> Result<(), DataError> {
        self.table.upsert_entity(contract_to_entity(record))
    }

    fn list(
        &self,
        underlying: Option<&str>,
        as_of: Option<Date>,
    ) -> Result<Vec<ContractRecord>, DataError> {
        let entities = self
            .table
            .query_entities(&TableQuery::partition_eq(CONTRACT_PARTITION_KEY))?;
        let mut records = entities
            .iter()
            .map(contract_from_entity)
            .collect::<Result<Vec<_>, _>>()?;
        records.sort_by(|left, right| left.symbol.cmp(&right.symbol));
        let underlying = underlying.map(canonical_symbol).transpose()?;
        records.retain(|record| {
            underlying
                .as_deref()
                .is_none_or(|filter| record.underlying == filter)
                && is_active(record, as_of)
        });
        Ok(records)
    }
}

/// Listing repository backed by an Azure Tables store.
pub struct AzureTableListingRepository {
    table: Arc<dyn TableEntityStore>,
}

impl AzureTableListingRepository {
    /// Build the repository over an injected table store.
    pub fn new(table: Arc<dyn TableEntityStore>) -> Self {
        Self { table }
    }
}

impl ListingRepository for AzureTableListingRepository {
    fn get(&self, entity_id: &str, symbol: &str) -> Result<Option<ListingRecord>, DataError> {
        let entity_id = entity_id.trim().to_uppercase();
        if entity_id.is_empty() {
            return Err(DataError::value("entity_id must not be empty"));
        }
        let symbol = canonical_symbol(symbol)?;
        match self
            .table
            .get_entity(&listing_partition_for(&entity_id), &table_key(&symbol))
        {
            Ok(entity) => Ok(Some(listing_from_entity(&entity)?)),
            Err(DataError::NotFound(_)) => Ok(None),
            Err(error) => Err(error),
        }
    }

    fn upsert(&self, record: &ListingRecord) -> Result<(), DataError> {
        self.table.upsert_entity(listing_to_entity(record))
    }

    fn list(
        &self,
        entity_id: Option<&str>,
        symbol: Option<&str>,
        as_of: Option<Date>,
    ) -> Result<Vec<ListingRecord>, DataError> {
        let entity_id = entity_id.map(str::trim).map(str::to_uppercase);
        if let Some(entity_id) = &entity_id {
            if entity_id.is_empty() {
                return Err(DataError::value("entity_id must not be empty"));
            }
        }
        let table_query = match &entity_id {
            Some(entity_id) => TableQuery::partition_eq(&listing_partition_for(entity_id)),
            None => TableQuery::partition_range("listings|", "listings}"),
        };
        let entities = self.table.query_entities(&table_query)?;
        let mut records = entities
            .iter()
            .map(listing_from_entity)
            .collect::<Result<Vec<_>, _>>()?;
        records.sort_by_key(|record| record.key());
        let symbol = symbol.map(canonical_symbol).transpose()?;
        records.retain(|record| {
            entity_id
                .as_deref()
                .is_none_or(|filter| record.entity_id == filter)
                && symbol
                    .as_deref()
                    .is_none_or(|filter| record.symbol == filter)
                && as_of.is_none_or(|filter| record.is_known(filter))
        });
        Ok(records)
    }
}

/// Extract the storage account name from a tables endpoint URL.
fn account_from_url(account_url: &str) -> Result<String, DataError> {
    let trimmed = account_url.trim().trim_end_matches('/');
    let rest = trimmed
        .split_once("://")
        .map(|(_, rest)| rest)
        .unwrap_or(trimmed);
    let host = rest
        .split(['/', '?', '#'])
        .next()
        .unwrap_or_default()
        .to_string();
    let account = host
        .strip_suffix(".table.core.windows.net")
        .unwrap_or(&host)
        .to_string();
    if account.is_empty() {
        return Err(DataError::value(
            "table endpoint must contain an account name",
        ));
    }
    Ok(account)
}

/// How SDK-backed adapters drive async SDK futures from sync ports.
///
/// A dedicated executor thread owns the Tokio runtime, so SDK calls are safe
/// from ordinary sync threads, from inside another runtime's workers, and
/// from test tasks, and the runtime is never dropped in a context where
/// blocking is not allowed.
#[derive(Clone)]
struct Executor {
    sender: std::sync::mpsc::Sender<Task>,
}

type Task = std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send + 'static>>;

impl Executor {
    fn shared() -> Self {
        static EXECUTOR: std::sync::OnceLock<Executor> = std::sync::OnceLock::new();
        EXECUTOR.get_or_init(Self::spawn).clone()
    }

    fn spawn() -> Self {
        let (sender, receiver) = std::sync::mpsc::channel::<Task>();
        let _ = std::thread::Builder::new()
            .name("azure-tables-executor".to_string())
            .spawn(move || {
                let Ok(runtime) = tokio::runtime::Builder::new_multi_thread()
                    .enable_all()
                    .build()
                else {
                    return;
                };
                for task in receiver {
                    runtime.block_on(task);
                }
            });
        Self { sender }
    }

    fn block_on<F>(&self, future: F) -> Result<F::Output, DataError>
    where
        F: std::future::Future + Send + 'static,
        F::Output: Send + 'static,
    {
        let (sender, receiver) = std::sync::mpsc::channel();
        let task: Task = Box::pin(async move {
            let output = future.await;
            let _ = sender.send(output);
        });
        self.sender
            .send(task)
            .map_err(|_| DataError::io("the azure tables executor is unavailable"))?;
        receiver
            .recv()
            .map_err(|_| DataError::io("the azure tables executor stopped"))
    }
}

fn is_http_status(error: &azure_core::Error, status: azure_core::StatusCode) -> bool {
    error
        .as_http_error()
        .is_some_and(|http_error| http_error.status() == status)
}

fn http_error(error: azure_core::Error) -> DataError {
    if is_http_status(&error, azure_core::StatusCode::NotFound) {
        return DataError::NotFound("missing".to_string());
    }
    DataError::Io(error.to_string())
}

/// SDK-backed table store over `azure_data_tables`.
///
/// Entities flow through as JSON objects; the named `@parameter` bindings are
/// inlined into the OData filter with single-quote escaping because the Rust
/// tables SDK 0.21 has no server-side parameter binding.
pub struct SdkTableEntityStore {
    table: azure_data_tables::clients::TableClient,
    executor: Executor,
}

impl SdkTableEntityStore {
    /// Build the store from a tables endpoint URL with a caller credential.
    pub fn from_account_url(
        account_url: &str,
        table_name: &str,
        credential: Arc<dyn azure_core::auth::TokenCredential>,
    ) -> Result<Self, DataError> {
        let account = account_from_url(account_url)?;
        let credentials = azure_storage::StorageCredentials::token_credential(credential);
        let service =
            azure_data_tables::clients::TableServiceClientBuilder::new(account, credentials)
                .build();
        Ok(Self {
            table: service.table_client(table_name),
            executor: Executor::shared(),
        })
    }

    /// Inline `@name` parameters into the OData filter string.
    fn inline_parameters(query: &TableQuery) -> String {
        let mut filter = query.filter.clone();
        for (name, value) in &query.parameters {
            let quoted = format!("'{}'", value.replace('\'', "''"));
            filter = filter.replace(&format!("@{name}"), &quoted);
        }
        filter
    }
}

impl TableEntityStore for SdkTableEntityStore {
    fn get_entity(&self, partition_key: &str, row_key: &str) -> Result<TableEntity, DataError> {
        let client = self
            .table
            .partition_key_client(partition_key)
            .entity_client(row_key);
        let response = self
            .executor
            .block_on(client.get::<Value>().into_future())?
            .map_err(http_error)?;
        response
            .entity
            .as_object()
            .cloned()
            .ok_or_else(|| DataError::value("table entity must be a JSON object"))
    }

    fn upsert_entity(&self, entity: TableEntity) -> Result<(), DataError> {
        let client = self.table.partition_key_client(
            entity
                .get("PartitionKey")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string(),
        );
        let row_key = entity
            .get("RowKey")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let future = client
            .entity_client(row_key)
            .insert_or_replace(entity)
            .map_err(http_error)?
            .into_future();
        self.executor
            .block_on(future)?
            .map(|_| ())
            .map_err(http_error)
    }

    fn query_entities(&self, query: &TableQuery) -> Result<Vec<TableEntity>, DataError> {
        use azure_data_tables::Filter;
        use futures_util::StreamExt;
        let filter = Self::inline_parameters(query);
        let stream = self
            .table
            .query()
            .filter(Filter::new(filter))
            .into_stream::<Value>();
        let pages = self.executor.block_on(stream.collect::<Vec<_>>())?;
        let mut entities = Vec::new();
        for page in pages {
            let page = page.map_err(http_error)?;
            for entity in page.entities {
                if let Some(object) = entity.as_object() {
                    entities.push(object.clone());
                }
            }
        }
        Ok(entities)
    }

    fn delete_entity(&self, partition_key: &str, row_key: &str) -> Result<(), DataError> {
        let client = self
            .table
            .partition_key_client(partition_key)
            .entity_client(row_key);
        let future = client.delete().into_future();
        self.executor
            .block_on(future)?
            .map(|_| ())
            .map_err(http_error)
    }
}
