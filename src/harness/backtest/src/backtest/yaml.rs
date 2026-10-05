//! Engine projection of the shared non-executable document parser.
use num::Decimal;
use std::collections::BTreeMap;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum YamlValue {
    Null,
    Bool(bool),
    Int(i64),
    Decimal(Decimal),
    String(String),
    Sequence(Vec<YamlValue>),
    Mapping(BTreeMap<String, YamlValue>),
}
impl YamlValue {
    pub fn as_mapping(&self) -> Option<&BTreeMap<String, YamlValue>> {
        match self {
            Self::Mapping(v) => Some(v),
            _ => None,
        }
    }
    pub fn as_sequence(&self) -> Option<&[YamlValue]> {
        match self {
            Self::Sequence(v) => Some(v),
            _ => None,
        }
    }
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Self::String(v) => Some(v),
            _ => None,
        }
    }
    pub fn as_int(&self) -> Option<i64> {
        match self {
            Self::Int(v) => Some(*v),
            _ => None,
        }
    }
    pub fn as_decimal(&self) -> Option<Decimal> {
        match self {
            Self::Decimal(v) => Some(*v),
            Self::Int(v) => Some(Decimal::from_i128(i128::from(*v))),
            Self::String(v) => v.parse().ok(),
            _ => None,
        }
    }
    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Self::Bool(v) => Some(*v),
            _ => None,
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct YamlError(pub String);
impl std::fmt::Display for YamlError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for YamlError {}

pub fn from_contract(value: &config::YamlValue) -> Result<YamlValue, YamlError> {
    Ok(match value {
        config::YamlValue::Null => YamlValue::Null,
        config::YamlValue::Bool(v) => YamlValue::Bool(*v),
        config::YamlValue::Int(v) => YamlValue::Int(*v),
        config::YamlValue::Decimal(v) => YamlValue::Decimal(
            v.parse()
                .map_err(|_| YamlError("decimal exceeds engine precision".into()))?,
        ),
        config::YamlValue::Str(v) => YamlValue::String(v.clone()),
        config::YamlValue::Seq(v) => {
            YamlValue::Sequence(v.iter().map(from_contract).collect::<Result<_, _>>()?)
        }
        config::YamlValue::Map(v) => YamlValue::Mapping(
            v.iter()
                .map(|(k, v)| Ok((k.clone(), from_contract(v)?)))
                .collect::<Result<_, YamlError>>()?,
        ),
    })
}
pub fn load_yaml(text: &str) -> Result<YamlValue, YamlError> {
    let mapping = config::replay::parse_document(text).map_err(|e| YamlError(e.0))?;
    from_contract(&config::YamlValue::Map(mapping))
}
