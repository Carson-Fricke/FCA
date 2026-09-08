"""Blinded RCA handoff: bundle assembly, response schema, advice, revisions.

This capability composes the authority-side protected release layer
(:mod:`thesis.protected`) into the four RCA handoff steps:

- :func:`thesis.rca.session.assemble_handoff` assembles the exactly
  policy-approved bundle for one bound release (full aliased run, original
  source as released, aliased contract/configuration, dependency versions,
  benchmark, costs, data-quality manifest, transformation manifest);
- :func:`thesis.rca.session.record_response` validates the RCA response
  against the diagnosis-only schema (candidate explanations, supporting
  and conflicting evidence, uncertainty, alternative explanations, and
  discriminating tests), failing closed on any other field;
- :class:`thesis.rca.egress.AdviceEgress` emits the schema-limited advice
  document for the main agent through the release's filtered-advice path;
  and
- :class:`thesis.rca.handoff.RevisionHandoff` returns an RCA-triggered
  revision to exploratory or validation data only, with sealed-window
  reuse barred and the strongest selection/reuse modifier recorded.
"""

from thesis.rca.egress import (
    ADVICE_SCHEMA,
    MAX_VERBATIM_RUN,
    AdviceDocument,
    AdviceEgress,
    AdviceEgressError,
)
from thesis.rca.handoff import (
    ALLOWED_REVISION_GRADES,
    BARRED_REVISION_GRADES,
    REVISION_HANDOFF_SCHEMA,
    STRONGEST_REUSE_MODIFIER,
    RevisionHandoff,
    RevisionHandoffDocument,
    RevisionHandoffError,
)
from thesis.rca.session import (
    BUNDLE_PAYLOAD_SECTIONS,
    BUNDLE_SCHEMA,
    RESPONSE_SCHEMA,
    RcaHandoffBundle,
    RcaHandoffError,
    RcaResponseError,
    RcaResponseField,
    RcaResponseVerdict,
    assemble_handoff,
    record_response,
    validate_rca_response,
)

__all__ = [
    "ADVICE_SCHEMA",
    "ALLOWED_REVISION_GRADES",
    "BARRED_REVISION_GRADES",
    "BUNDLE_PAYLOAD_SECTIONS",
    "BUNDLE_SCHEMA",
    "MAX_VERBATIM_RUN",
    "RESPONSE_SCHEMA",
    "REVISION_HANDOFF_SCHEMA",
    "RcaHandoffBundle",
    "RcaHandoffError",
    "RcaResponseError",
    "RcaResponseField",
    "RcaResponseVerdict",
    "STRONGEST_REUSE_MODIFIER",
    "AdviceDocument",
    "AdviceEgress",
    "AdviceEgressError",
    "RevisionHandoff",
    "RevisionHandoffDocument",
    "RevisionHandoffError",
    "assemble_handoff",
    "record_response",
    "validate_rca_response",
]
