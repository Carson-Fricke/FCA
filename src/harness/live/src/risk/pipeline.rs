//! Ordered, synchronous risk evaluation.

use crate::error::{LiveError, LiveResult};
use crate::orders::OrderRequest;
use crate::risk::models::{RiskAction, RiskAudit, RiskContext, RiskDecision, RiskRule};

/// The action taken when a rule rejects an order.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum OnReject {
    /// Block the order only.
    RejectOrder,
    /// Halt the submitting strategy.
    HaltStrategy,
    /// Abort the whole session.
    HaltRuntime,
}

impl OnReject {
    /// Parse the canonical policy name.
    pub fn parse(value: &str) -> LiveResult<Self> {
        match value {
            "reject_order" => Ok(Self::RejectOrder),
            "halt_strategy" => Ok(Self::HaltStrategy),
            "halt_runtime" => Ok(Self::HaltRuntime),
            _ => Err(LiveError::Risk(
                "on_reject must be reject_order, halt_strategy, or halt_runtime".into(),
            )),
        }
    }

    fn halt_scope(self) -> Option<String> {
        match self {
            Self::RejectOrder => None,
            Self::HaltStrategy => Some("halt_strategy".into()),
            Self::HaltRuntime => Some("halt_runtime".into()),
        }
    }
}

/// Evaluate orders through rules in stable configured order.
///
/// A reduction restarts at the first rule. This makes every earlier rule
/// re-check the changed order and records both the reduction and all rechecks.
/// The pipeline has no implicit state, time source, randomness, or I/O.
pub struct RiskPipeline {
    rules: Vec<Box<dyn RiskRule>>,
    on_reject: OnReject,
}

impl RiskPipeline {
    /// Construct a pipeline with at least one rule.
    pub fn new(rules: Vec<Box<dyn RiskRule>>, on_reject: OnReject) -> LiveResult<Self> {
        if rules.is_empty() {
            return Err(LiveError::Risk(
                "risk pipeline requires at least one rule".into(),
            ));
        }
        for rule in &rules {
            if rule.rule_id().trim().is_empty() {
                return Err(LiveError::Risk(
                    "risk rules require a non-empty rule_id".into(),
                ));
            }
        }
        Ok(Self { rules, on_reject })
    }

    /// Return configured rules in evaluation order.
    pub fn rules(&self) -> &[Box<dyn RiskRule>] {
        &self.rules
    }

    /// Return the configured rejection policy.
    pub fn on_reject(&self) -> OnReject {
        self.on_reject
    }

    /// Return a complete deterministic decision for one candidate order.
    pub fn evaluate(
        &self,
        order: &OrderRequest,
        context: &RiskContext,
    ) -> LiveResult<RiskDecision> {
        let original = order.clone();
        let mut current = order.clone();
        let mut audits: Vec<RiskAudit> = Vec::new();
        let mut reasons: Vec<String> = Vec::new();
        let mut pass_number = 1usize;
        let mut index = 0usize;

        while index < self.rules.len() {
            let rule = &self.rules[index];
            let before = current.clone();
            let current_recheck = pass_number > 1;
            let result = rule.evaluate(&current, context)?;
            let action = result.action;
            let after = if action == RiskAction::Reduce {
                result.order.clone()
            } else {
                current.clone()
            };
            let reason = result.reason.clone();
            audits.push(RiskAudit {
                sequence: audits.len(),
                pass_number,
                rule_id: rule.rule_id().to_string(),
                action,
                order_before: before,
                order_after: after,
                reason: reason.clone(),
                metrics: result.metrics.clone(),
                recheck: current_recheck,
                halt: result.halt,
            });
            if let Some(reason) = &reason {
                reasons.push(reason.clone());
            }
            match action {
                RiskAction::Reject => {
                    return Ok(self.decision(original, current, audits, reasons, context, reason));
                }
                RiskAction::Reduce => {
                    if result.order == current {
                        let message = format!("{} returned an unchanged reduction", rule.rule_id());
                        reasons.push(message.clone());
                        audits.push(RiskAudit {
                            sequence: audits.len(),
                            pass_number,
                            rule_id: rule.rule_id().to_string(),
                            action: RiskAction::Reject,
                            order_before: current.clone(),
                            order_after: current.clone(),
                            reason: Some(message.clone()),
                            metrics: result.metrics.clone(),
                            recheck: current_recheck,
                            halt: false,
                        });
                        return Ok(self.decision(
                            original,
                            current,
                            audits,
                            reasons,
                            context,
                            Some(message),
                        ));
                    }
                    current = result.order;
                    pass_number += 1;
                    index = 0;
                }
                RiskAction::Allow => index += 1,
            }
        }

        let action = if current != original {
            RiskAction::Reduce
        } else {
            RiskAction::Allow
        };
        Ok(RiskDecision {
            action,
            original_order: original,
            order: current,
            audits,
            reason: if action == RiskAction::Reduce {
                reasons.last().cloned()
            } else {
                None
            },
            reasons,
            strategy_id: context.strategy_id.clone(),
            halt_scope: None,
        })
    }

    fn decision(
        &self,
        original: OrderRequest,
        current: OrderRequest,
        audits: Vec<RiskAudit>,
        reasons: Vec<String>,
        context: &RiskContext,
        reason: Option<String>,
    ) -> RiskDecision {
        RiskDecision {
            action: RiskAction::Reject,
            original_order: original,
            order: current,
            audits,
            reason,
            reasons,
            strategy_id: context.strategy_id.clone(),
            halt_scope: self.on_reject.halt_scope(),
        }
    }
}

/// Compatibility alias for the pipeline engine.
pub type RiskEngine = RiskPipeline;
