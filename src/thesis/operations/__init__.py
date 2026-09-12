"""Thesis operational hardening: security boundaries, recovery, integrity.

This package collects the Phase 6 operational contracts that make local and
Azure Thesis runtimes safe to operate together:

* :mod:`thesis.operations.security` enforces the workspace, event, artifact,
  consent, and audit boundaries, and screens prompt injection, secret
  exfiltration, confused-deputy tool use, and provider-authentication failure.
* :mod:`thesis.operations.recovery` classifies checkpoint, queue, Git
  publication, and partial tool-completion recovery read-only and delegates the
  actual mutations to the existing execution and persistence primitives.
* :mod:`thesis.operations.integrity` measures and enforces all-in-Git
  repository budgets and provides deterministic backup, restore, and retention.
"""

from thesis.operations.integrity import (
    ARTIFACT_ROOTS,
    BackupIntegrityError,
    BackupReceipt,
    FilesystemRepositorySize,
    IntegrityLimitError,
    IntegrityReport,
    RepositoryBudget,
    RepositoryBudgetExceeded,
    RepositoryMeasurement,
    RepositorySizePort,
    RetentionPlan,
    RetentionPolicy,
    create_backup,
    enforce_budget,
    measure_repository,
    plan_retention,
    restore_backup,
)
from thesis.operations.recovery import (
    PublicationLookup,
    RecoveryAction,
    RecoveryDecision,
    RecoveryError,
    RecoveryPlan,
    RecoveryPlanner,
    RecoveryScenario,
)
from thesis.operations.security import (
    BoundaryCheck,
    BoundaryKind,
    ConfusedDeputyError,
    ModeParityReport,
    SecretExfiltrationError,
    SecurityBoundaryError,
    SecurityError,
    assert_local_azure_parity,
    assert_no_secret_material,
    assert_tool_permitted,
    check_artifact_boundary,
    check_audit_boundary,
    check_consent_boundary,
    check_event_boundary,
    check_workspace_boundary,
    classify_provider_auth,
    scan_untrusted_text,
)

__all__ = [
    "ARTIFACT_ROOTS",
    "BackupIntegrityError",
    "BackupReceipt",
    "BoundaryCheck",
    "BoundaryKind",
    "ConfusedDeputyError",
    "FilesystemRepositorySize",
    "IntegrityLimitError",
    "IntegrityReport",
    "ModeParityReport",
    "PublicationLookup",
    "RecoveryAction",
    "RecoveryDecision",
    "RecoveryError",
    "RecoveryPlan",
    "RecoveryPlanner",
    "RecoveryScenario",
    "RepositoryBudget",
    "RepositoryBudgetExceeded",
    "RepositoryMeasurement",
    "RepositorySizePort",
    "RetentionPlan",
    "RetentionPolicy",
    "SecretExfiltrationError",
    "SecurityBoundaryError",
    "SecurityError",
    "assert_local_azure_parity",
    "assert_no_secret_material",
    "assert_tool_permitted",
    "check_artifact_boundary",
    "check_audit_boundary",
    "check_consent_boundary",
    "check_event_boundary",
    "check_workspace_boundary",
    "classify_provider_auth",
    "create_backup",
    "enforce_budget",
    "measure_repository",
    "plan_retention",
    "restore_backup",
    "scan_untrusted_text",
]
