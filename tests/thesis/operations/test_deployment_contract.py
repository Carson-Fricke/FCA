"""Thesis Azure deployment contract.

The contract is pinned without touching Azure: the Bicep modules are checked
as text for the image-digest, identity, secretRef, probe, scale, isolation,
and cost envelope requirements, and the deploy workflow is parsed as YAML to
verify the OIDC posture, environment gating, and probe-safe smoke steps. The
first real deployment happens in the deploy-thesis workflow, not in these
tests. Validation is read-only; no live Azure mutation occurs.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import cast

import pytest

ROOT = Path(__file__).parents[3]
THESIS_BICEP = ROOT / "infra" / "modules" / "thesis.bicep"
THESIS_WORKSPACES_BICEP = ROOT / "infra" / "modules" / "thesis-workspaces.bicep"
MAIN_BICEP = ROOT / "infra" / "main.bicep"
THESIS_DEV_PARAMS = ROOT / "infra" / "parameters" / "thesis.dev.bicepparam"
DEPLOY_WORKFLOW = ROOT / ".github" / "workflows" / "deploy-thesis.yml"

AZURE_LOGIN_PIN = "eec3c95657c1536435858eda1f3ff5437fee8474"
CHECKOUT_PIN = "11bd71901bbe5b1630ceea73d27597364c9af683"
BEARER_SECRET_NAME = "thesis-api-bearer-token"

_SECRET_WORD_PATTERN = re.compile(
    r"\b(?:access[_-]?key|api[_-]?key|api[_-]?secret|bearer|"
    r"client[_-]?secret|credential|password|private[_-]?key|"
    r"secret|token)s?\b",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT_PATTERN = re.compile(r"\bvalue:\s*'", re.IGNORECASE)
_WORKFLOW_ALLOWED_SECRETS = frozenset({"GITHUB_TOKEN", "BUDGET_ALERT_CONTACT_EMAIL"})


def _read(path: Path) -> str:
    assert path.is_file(), f"expected file to exist: {path}"
    return path.read_text(encoding="utf-8")


def _code_without_bicep_comments(text: str) -> str:
    """Strip trailing ``//`` comments without truncating ``https://`` URLs."""
    return "\n".join(re.sub(r"(?<!:)//.*$", "", line) for line in text.splitlines())


def _assert_no_plain_secret_values(text: str, label: str) -> None:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        code = re.sub(r"(?<!:)//.*$", "", line)
        if _SECRET_ASSIGNMENT_PATTERN.search(code):
            window = "\n".join(lines[max(0, index - 3) : index + 4])
            if _SECRET_WORD_PATTERN.search(window):
                pytest.fail(
                    f"{label} line {index + 1} assigns a literal value near a "
                    f"secret-looking name; secretRefs are required"
                )


def _load_workflow_yaml() -> dict[object, object]:
    """Parse the workflow with PyYAML when the environment provides it."""
    try:
        yaml = importlib.import_module("yaml")
    except ModuleNotFoundError:
        pytest.skip("pyyaml is not installed for workflow YAML parsing")
    loaded = yaml.safe_load(_read(DEPLOY_WORKFLOW))
    assert isinstance(loaded, dict), "workflow must parse to a mapping"
    # PyYAML resolves unquoted ``on`` trigger keys to the boolean True, so
    # the mapping is typed with object keys for the trigger lookup.
    return cast("dict[object, object]", loaded)


# ---------------------------------------------------------------------------
# Thesis API Container App module
# ---------------------------------------------------------------------------


def test_thesis_api_image_reference_is_digest_pinned() -> None:
    text = _read(THESIS_BICEP)

    assert "param thesisImageReference string" in text
    assert "image: thesisImageReference" in text
    assert "contains(thesisImageReference, '@sha256:')" in text
    assert "@description('Immutable digest-pinned private ACR image reference" in text


def test_thesis_api_consumes_the_bearer_token_through_a_secret_ref() -> None:
    text = _read(THESIS_BICEP)

    assert "var thesisSecretName = 'thesis-api-bearer-token'" in text
    assert "name: thesisSecretName" in text
    assert "keyVaultUrl: '${keyVaultUri}secrets/${thesisSecretName}'" in text
    assert "name: 'THESIS_API__BEARER_TOKEN'" in text
    assert "secretRef: thesisSecretName" in text


def test_thesis_modules_commit_no_plain_secret_values() -> None:
    for path in (THESIS_BICEP, THESIS_WORKSPACES_BICEP, MAIN_BICEP):
        _assert_no_plain_secret_values(_read(path), path.name)


def test_thesis_api_probes_health() -> None:
    text = _read(THESIS_BICEP)

    assert text.count("type: 'Startup'") == 1
    assert text.count("type: 'Liveness'") == 1
    assert text.count("type: 'Readiness'") == 1
    assert text.count("path: '/health'") == 3
    assert text.count("port: thesisContainerPort") >= 3
    assert "var thesisContainerPort = 8000" in text


def test_thesis_api_scales_to_zero_by_default() -> None:
    text = _read(THESIS_BICEP)

    assert "param thesisMinReplicas int = 0" in text
    assert "minReplicas: thesisMinReplicas" in text
    assert "maxReplicas: thesisMaxReplicas" in text
    assert "name: 'http-requests'" in text


def test_thesis_api_uses_a_parameterized_managed_identity() -> None:
    text = _read(THESIS_BICEP)

    assert "param thesisIdentityResourceId string" in text
    assert "param thesisIdentityClientId string" in text
    assert "'${thesisIdentityResourceId}': {}" in text
    assert "identity: thesisIdentityResourceId" in text
    assert "server: registryLoginServer" in text
    # The module must not create an identity; the composition owns it.
    assert "Microsoft.ManagedIdentity/userAssignedIdentities@" not in text


def test_thesis_api_streams_diagnostics_to_the_existing_workspace() -> None:
    text = _read(THESIS_BICEP)

    assert "Microsoft.Insights/diagnosticSettings@2021-05-01-preview" in text
    assert "categoryGroup: 'allLogs'" in text
    assert "workspaceId: logAnalyticsWorkspaceResourceId" in text
    assert "param logAnalyticsWorkspaceResourceId string" in text


def test_thesis_api_outputs_expose_the_operations_surface() -> None:
    text = _read(THESIS_BICEP)

    assert "output thesisApi object = {" in text
    assert "fqdn: thesisApiApp.properties.configuration.ingress.fqdn" in text
    assert "output thesisIdentity object = {" in text
    assert "resourceId: thesisIdentityResourceId" in text


def test_thesis_api_documents_the_additive_cost_envelope() -> None:
    text = _read(THESIS_BICEP)

    assert "Additive monthly cost envelope" in text
    assert "scale-to-zero" in text
    assert "maxReplicas" in text
    assert "PerGB2018" in text
    assert "output costEnvelope object = {" in text


# ---------------------------------------------------------------------------
# Thesis workspace isolation module
# ---------------------------------------------------------------------------


def test_workspace_persistence_is_a_dedicated_locked_down_account() -> None:
    text = _read(THESIS_WORKSPACES_BICEP)

    assert "kind: 'StorageV2'" in text
    assert "name: 'Standard_LRS'" in text
    assert "allowSharedKeyAccess: false" in text
    assert "defaultToOAuthAuthentication: true" in text
    assert "allowBlobPublicAccess: false" in text
    assert "minimumTlsVersion: 'TLS1_2'" in text


def test_workspace_isolation_model_is_container_per_workspace() -> None:
    text = _read(THESIS_WORKSPACES_BICEP)

    assert "isolation model" in text.lower()
    assert "Container-per-workspace" in text
    assert "var workspaceContainerPrefix = 'thesis-ws-'" in text
    assert "var workspaceRegistryTableName = 'workspaces'" in text
    assert "param workspaceRegistry array = []" in text
    assert "publicAccess: 'None'" in text


def test_workspace_registry_binds_the_azure_entity_contract() -> None:
    text = _read(THESIS_WORKSPACES_BICEP)

    assert "azure_contract.py" in text
    assert "PartitionKey" in text
    assert "ExecutionScopeId" in text
    assert "persistenceScopeId" in text


def test_workspace_runtime_access_is_least_privilege() -> None:
    text = _read(THESIS_WORKSPACES_BICEP)

    assert "ba92f5b4-2d11-453d-a403-e96b0029c9fe" in text
    assert "0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3" in text
    # Thesis workspaces never touch queues.
    assert "974c5e8b-45b9-4653-ba55-5f855dd0fb88" not in text


# ---------------------------------------------------------------------------
# Composition and dev parameters
# ---------------------------------------------------------------------------


def test_main_composition_keeps_thesis_additive_and_disabled_by_default() -> None:
    text = _read(MAIN_BICEP)

    assert "param deployThesis bool = false" in text
    assert (
        "module thesisWorkspaces './modules/thesis-workspaces.bicep'"
        " = if (deployThesis) {" in text
    )
    assert "module thesisApi './modules/thesis.bicep' = if (deployThesis) {" in text
    assert "enabled: deployThesis" in text
    # The identity is parameterized into the modules, not created by them.
    assert "resource thesisIdentity" in text
    assert "thesisIdentityResourceId: deployThesis ? thesisIdentity.id" in text


def test_dev_parameters_enable_thesis_with_a_digest_pinned_sentinel() -> None:
    text = _read(THESIS_DEV_PARAMS)

    assert "param deployThesis = true" in text
    assert re.search(r"@sha256:[0-9a-f]{64}", text)
    assert "param thesisEnableExternalIngress = false" in text
    assert "workspaceId: 'equity-momentum'" in text


# ---------------------------------------------------------------------------
# Deploy workflow
# ---------------------------------------------------------------------------


def test_workflow_pins_actions_and_gates_on_the_environment() -> None:
    text = _read(DEPLOY_WORKFLOW)

    assert f"azure/login@{AZURE_LOGIN_PIN}" in text
    assert f"actions/checkout@{CHECKOUT_PIN}" in text
    assert text.count("id-token: write") >= 3
    assert "${{ vars.DEPLOYMENT_ENVIRONMENT }}" in text


def test_workflow_references_no_uncommitted_secrets() -> None:
    text = _read(DEPLOY_WORKFLOW)

    referenced = set(re.findall(r"secrets\.([A-Z_]+)", text))
    assert referenced <= _WORKFLOW_ALLOWED_SECRETS
    _assert_no_plain_secret_values(text, DEPLOY_WORKFLOW.name)


def test_workflow_smoke_verifies_health_and_the_auth_boundary() -> None:
    text = _read(DEPLOY_WORKFLOW)

    assert "/health" in text
    assert "/v1/auth/check" in text
    assert BEARER_SECRET_NAME in text
    assert "restore_scale_to_zero" in text
    assert "trap restore_scale_to_zero EXIT" in text
    assert "trap - EXIT" in text
    assert "--min-replicas 1" in text
    assert "--min-replicas 0" in text


def test_workflow_deploys_the_thesis_modules_with_a_digest_pinned_image() -> None:
    text = _read(DEPLOY_WORKFLOW)

    assert "infra/main.bicep" in text
    assert "deployThesis=true" in text
    assert "thesisImageReference=$THESIS_IMAGE_REFERENCE" in text
    assert re.search(r"sha256:\[0-9a-f\]\{64\}", text)


def test_workflow_yaml_declares_the_thesis_job_chain() -> None:
    workflow = _load_workflow_yaml()

    assert workflow["name"] == "Deploy thesis environment"
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert set(jobs) == {"build-thesis-image", "deploy", "deploy-ui", "thesis-smoke"}

    # PyYAML 1.1 resolves the unquoted ``on`` key to the boolean True.
    triggers = workflow["on"] if "on" in workflow else workflow[True]
    assert isinstance(triggers, dict)
    push = triggers["push"]
    assert isinstance(push, dict)
    paths = push["paths"]
    assert isinstance(paths, list)
    assert "Dockerfile.thesis" in paths

    for name in ("build-thesis-image", "deploy", "deploy-ui", "thesis-smoke"):
        job = jobs[name]
        assert isinstance(job, dict)
        permissions = job["permissions"]
        assert isinstance(permissions, dict)
        assert permissions["contents"] == "read"
        assert permissions["id-token"] == "write"
        environment = job["environment"]
        assert isinstance(environment, dict)
        assert environment["name"] == "${{ vars.DEPLOYMENT_ENVIRONMENT }}"
