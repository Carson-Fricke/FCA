"""Thesis public-domain hosting and TLS contract.

The contract is pinned without touching Azure or the registrar: the Thesis
Bicep module must keep the custom hostname empty by default and gate the
managed certificate on that parameter, the composition must pass it through
additively, and the deploy workflow must gate the hostname behind operator
variables while the custom-domain smoke skips cleanly when they are unset.
The registrar-facing DNS steps stay in the runbook. Validation is read-only;
no live Azure mutation or registrar access occurs.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import cast

import pytest

ROOT = Path(__file__).parents[3]
THESIS_BICEP = ROOT / "infra" / "modules" / "thesis.bicep"
MAIN_BICEP = ROOT / "infra" / "main.bicep"
DEPLOY_WORKFLOW = ROOT / ".github" / "workflows" / "deploy-thesis.yml"
DOMAIN_RUNBOOK = ROOT / ".github" / "runbooks" / "domain-hosting.md"

CUSTOM_DOMAIN_VAR = "THESIS_CUSTOM_DOMAIN"
AUTHORIZED_VAR = "THESIS_CUSTOM_DOMAIN_AUTHORIZED"
CHECK_VAR = "THESIS_CHECK_CUSTOM_DOMAIN"
CUSTOM_DOMAIN_PARAM = "customDomainName"
MAIN_DOMAIN_PARAM = "thesisCustomDomain"

# Hostname literals must not be committed into the infrastructure: the
# operator's domain reaches Azure only through workflow variables. The
# registry suffix is allowlisted because the digest-pinned image sentinel
# legitimately references the private ACR.
_HOSTNAME_LITERAL_PATTERN = re.compile(
    r"\b[a-z0-9][a-z0-9-]*\.(?:com|net|io|org|dev|azure)\b",
    re.IGNORECASE,
)
_ALLOWED_HOSTNAME_LITERALS = frozenset({"azurecr.io"})
_SECRET_ASSIGNMENT_PATTERN = re.compile(r"\bvalue:\s*'", re.IGNORECASE)
_SECRET_WORD_PATTERN = re.compile(
    r"\b(?:access[_-]?key|api[_-]?key|api[_-]?secret|bearer|"
    r"client[_-]?secret|credential|password|private[_-]?key|"
    r"secret|token)s?\b",
    re.IGNORECASE,
)


def _read(path: Path) -> str:
    assert path.is_file(), f"expected file to exist: {path}"
    return path.read_text(encoding="utf-8")


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


def _job_env(workflow: dict[object, object], job_name: str) -> dict[object, object]:
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict), "workflow jobs must be a mapping"
    job = jobs[job_name]
    assert isinstance(job, dict), f"job {job_name} must be a mapping"
    env = job.get("env", {})
    assert isinstance(env, dict), f"job {job_name} env must be a mapping"
    return cast("dict[object, object]", env)


def _hostname_literals(text: str) -> set[str]:
    return {
        match.group(0).lower()
        for match in _HOSTNAME_LITERAL_PATTERN.finditer(text)
        if match.group(0).lower() not in _ALLOWED_HOSTNAME_LITERALS
    }


def _assert_no_plain_secret_values(text: str, label: str) -> None:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        code = re.sub(r"(?<!:)//.*$", "", line)
        if _SECRET_ASSIGNMENT_PATTERN.search(code):
            window = "\n".join(lines[max(0, index - 3) : index + 4])
            if _SECRET_WORD_PATTERN.search(window):
                pytest.fail(
                    f"{label} line {index + 1} assigns a literal value near a "
                    f"secret-looking name"
                )


# ---------------------------------------------------------------------------
# Thesis module: parameterized custom domain and managed certificate
# ---------------------------------------------------------------------------


def test_custom_domain_is_optional_and_empty_by_default() -> None:
    text = _read(THESIS_BICEP)

    assert f"param {CUSTOM_DOMAIN_PARAM} string = ''" in text
    assert "@description('Optional public custom hostname" in text
    # Empty preserves the pre-domain deployment exactly: no hostname binding.
    assert "customDomains: empty(customDomainName) ? [] : [" in text
    assert "bindingType: 'SniEnabled'" in text
    assert f"name: {CUSTOM_DOMAIN_PARAM}" in text


def test_managed_certificate_is_gated_on_the_custom_domain_parameter() -> None:
    text = _read(THESIS_BICEP)

    assert (
        "resource thesisManagedCertificate "
        "'Microsoft.App/managedEnvironments/managedCertificates@" in text
    )
    # The certificate resource is skipped entirely for the default deployment.
    assert "= if (!empty(customDomainName)) {" in text
    assert "subjectName: customDomainName" in text
    assert "domainControlValidation: 'CNAME'" in text
    # The app binding references the certificate resource, so the ARM
    # deployment orders the certificate before the container app.
    assert "certificateId: thesisManagedCertificate.id" in text
    # The environment is referenced, never created or duplicated.
    assert (
        "resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@" in text
    )
    assert "existing = {" in text


def test_domain_wiring_commits_no_plaintext_hostnames_or_secrets() -> None:
    for path in (THESIS_BICEP, MAIN_BICEP):
        text = _read(path)
        literals = _hostname_literals(text)
        assert literals == set(), (
            f"{path.name} commits hostname literals instead of parameters: "
            f"{sorted(literals)}"
        )
        _assert_no_plain_secret_values(text, path.name)


def test_thesis_module_reports_the_custom_domain_in_its_output() -> None:
    text = _read(THESIS_BICEP)

    assert "customDomain: customDomainName" in text


# ---------------------------------------------------------------------------
# Composition passthrough
# ---------------------------------------------------------------------------


def test_main_composition_passes_the_custom_domain_through_additively() -> None:
    text = _read(MAIN_BICEP)

    assert f"param {MAIN_DOMAIN_PARAM} string = ''" in text
    assert f"{CUSTOM_DOMAIN_PARAM}: {MAIN_DOMAIN_PARAM}" in text
    # The passthrough must stay inside the existing conditional module; the
    # default deployment composition is unchanged.
    assert "module thesisApi './modules/thesis.bicep' = if (deployThesis) {" in text


# ---------------------------------------------------------------------------
# Deploy workflow gating
# ---------------------------------------------------------------------------


def test_workflow_declares_the_custom_domain_operator_variables() -> None:
    text = _read(DEPLOY_WORKFLOW)

    assert f"{CUSTOM_DOMAIN_VAR}: ${{{{ vars.{CUSTOM_DOMAIN_VAR} }}}}" in text
    assert f"{AUTHORIZED_VAR}: ${{{{ vars.{AUTHORIZED_VAR} }}}}" in text
    assert f"{CHECK_VAR}: ${{{{ vars.{CHECK_VAR} }}}}" in text
    assert f"thesisCustomDomain=${{{CUSTOM_DOMAIN_VAR}}}" in text


def test_workflow_requires_the_authorization_acknowledgment() -> None:
    text = _read(DEPLOY_WORKFLOW)

    # The domain must not bind without the recorded acknowledgment.
    assert (
        "THESIS_CUSTOM_DOMAIN is set but THESIS_CUSTOM_DOMAIN_AUTHORIZED "
        "is not exactly true" in text
    )
    # A stray non-true acknowledgment value must fail closed.
    assert "THESIS_CUSTOM_DOMAIN_AUTHORIZED must be empty or exactly true" in text
    # The authorization requires the external-ingress posture decision.
    assert "A custom domain requires external ingress" in text


def test_workflow_custom_domain_smoke_skips_when_unset() -> None:
    text = _read(DEPLOY_WORKFLOW)

    # Skip-cleanly behavior: an unset domain produces no failure and no
    # request, so the default deployment is unaffected.
    assert "THESIS_CUSTOM_DOMAIN is unset; skipping the custom-hostname smoke" in text
    # An explicitly enabled check without a domain is a misconfiguration.
    assert (
        "THESIS_CHECK_CUSTOM_DOMAIN is true but THESIS_CUSTOM_DOMAIN is empty" in text
    )
    # The smoke exercises HTTPS and the anonymous auth boundary.
    assert "https://${THESIS_CUSTOM_DOMAIN}/health" in text
    assert "https://${THESIS_CUSTOM_DOMAIN}/v1/auth/check" in text
    assert "instead of 200" in text
    assert "instead of rejecting anonymous access" in text


def test_workflow_yaml_declares_the_gating_and_smoke_environment() -> None:
    workflow = _load_workflow_yaml()

    assert workflow["name"] == "Deploy thesis environment"
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert set(jobs) == {"build-thesis-image", "deploy", "thesis-smoke"}

    deploy_env = _job_env(workflow, "deploy")
    for variable in (CUSTOM_DOMAIN_VAR, AUTHORIZED_VAR):
        expected = f"${{{{ vars.{variable} }}}}"
        assert expected in deploy_env.values(), (
            f"deploy job must map {variable} from {expected}"
        )

    smoke_env = _job_env(workflow, "thesis-smoke")
    for variable in (CUSTOM_DOMAIN_VAR, CHECK_VAR):
        expected = f"${{{{ vars.{variable} }}}}"
        assert expected in smoke_env.values(), (
            f"thesis-smoke job must map {variable} from {expected}"
        )


# ---------------------------------------------------------------------------
# Registrar runbook
# ---------------------------------------------------------------------------


def test_runbook_documents_the_dns_records_and_az_commands() -> None:
    text = _read(DOMAIN_RUNBOOK)

    # The managed-certificate validation record set and the routing record.
    assert "asuid" in text
    assert "CNAME" in text
    assert "TXT" in text
    # The copy-executable command sequence (managed certificate, no upload).
    assert "az containerapp hostname add" in text
    assert "az containerapp hostname bind" in text
    assert "--validation-method CNAME" in text
    # The operator acknowledgment variables and verification queries.
    assert CUSTOM_DOMAIN_VAR in text
    assert AUTHORIZED_VAR in text
    assert "customDomainVerificationId" in text
    # No GitHub secret references belong in a registrar runbook.
    assert not re.findall(r"secrets\.[A-Z_]+", text)
