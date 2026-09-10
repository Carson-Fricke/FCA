//! Shadow-mode intent comparison.
//!
//! Deterministic core of `harness.runtime.shadow`: compare the broker intents
//! a live feed would produce against the intents the strategy produced, and
//! report agreement or the first divergence. The observe-only broker sink and
//! shell wiring stay behind ports.

/// The shadow comparison verdict.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ShadowVerdict {
    /// Every compared intent agreed.
    Agree,
    /// At least one intent diverged.
    Diverge,
}

impl ShadowVerdict {
    /// The stable lowercase value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Agree => "agree",
            Self::Diverge => "diverge",
        }
    }
}

/// One intended or observed broker intent.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BrokerIntent {
    /// Order identifier.
    pub order_id: String,
    /// Instrument symbol.
    pub symbol: String,
    /// Order side.
    pub side: String,
    /// String-exact quantity.
    pub quantity: String,
}

/// One live print observed during a shadow session.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LivePrint {
    /// Instrument symbol.
    pub symbol: String,
    /// String-exact price.
    pub price: String,
    /// String-exact quantity.
    pub quantity: String,
}

/// One intent-by-intent comparison result.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct IntentComparison {
    /// Index of the first divergence, if any.
    pub first_difference: Option<usize>,
    /// Number of intents compared.
    pub compared: usize,
}

/// Compare expected and observed intents in order.
#[must_use]
pub fn compare_intents(expected: &[BrokerIntent], actual: &[BrokerIntent]) -> IntentComparison {
    let compared = expected.len().min(actual.len());
    let mut first_difference = if expected.len() == actual.len() {
        None
    } else {
        Some(compared)
    };
    for index in 0..compared {
        if expected[index] != actual[index] {
            first_difference = Some(index);
            break;
        }
    }
    IntentComparison {
        first_difference,
        compared,
    }
}

/// The verdict for one comparison.
#[must_use]
pub fn verdict(comparison: &IntentComparison) -> ShadowVerdict {
    if comparison.first_difference.is_none() {
        ShadowVerdict::Agree
    } else {
        ShadowVerdict::Diverge
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn intent(id: &str, quantity: &str) -> BrokerIntent {
        BrokerIntent {
            order_id: id.to_string(),
            symbol: "SPY".to_string(),
            side: "buy".to_string(),
            quantity: quantity.to_string(),
        }
    }

    #[test]
    fn agrees_on_identical_intents() {
        let expected = vec![intent("o1", "10")];
        let comparison = compare_intents(&expected, &expected.clone());
        assert_eq!(verdict(&comparison), ShadowVerdict::Agree);
        assert_eq!(comparison.compared, 1);
    }

    #[test]
    fn reports_first_divergence() {
        let expected = vec![intent("o1", "10"), intent("o2", "5")];
        let actual = vec![intent("o1", "10"), intent("o2", "6")];
        let comparison = compare_intents(&expected, &actual);
        assert_eq!(comparison.first_difference, Some(1));
        assert_eq!(verdict(&comparison), ShadowVerdict::Diverge);
    }

    #[test]
    fn reports_length_divergence() {
        let expected = vec![intent("o1", "10")];
        let actual = vec![intent("o1", "10"), intent("o2", "5")];
        assert_eq!(
            compare_intents(&expected, &actual).first_difference,
            Some(1)
        );
    }
}
