"""Deployment, order, portfolio, and risk-decision HTTP surfaces.

Deployment cards and order records are workspace-local persistence; the
portfolio and risk-decision surfaces are consumed through the harness
status-client seam (:class:`RuntimeStatusClient`) and fail closed with 503
when the harness has not published them — no substitute data is fabricated.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from thesis.integrations.harness_activation import HarnessApiResponse
from thesis.integrations.runtime_status import (
    HarnessStatusError,
    PortfolioSummary,
    RiskDecisionRecord,
    RuntimeStatusClient,
    StatusSurface,
)


class DeploymentStore(Protocol):
    """Read boundary over locally persisted deployment and order records."""

    def list_deployments(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        """Return every persisted deployment card for the workspace."""
        ...

    def list_orders(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        """Return every persisted order record for the workspace."""
        ...


class InMemoryDeploymentStore:
    """Local in-memory deployment and order store."""

    def __init__(
        self,
        deployments: tuple[dict[str, object], ...] = (),
        orders: tuple[dict[str, object], ...] = (),
    ) -> None:
        self._deployments: dict[str, list[dict[str, object]]] = {}
        self._orders: dict[str, list[dict[str, object]]] = {}
        for card in deployments:
            self._deployments.setdefault(str(card["workspace_id"]), []).append(
                dict(card)
            )
        for order in orders:
            self._orders.setdefault(str(order["workspace_id"]), []).append(dict(order))

    def list_deployments(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        return tuple(dict(card) for card in self._deployments.get(workspace_id, ()))

    def list_orders(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        return tuple(dict(order) for order in self._orders.get(workspace_id, ()))


class _UnavailableTransport:
    """Fail-closed transport used when no harness endpoint is configured."""

    def get(self, url: str, headers: Mapping[str, str]) -> HarnessApiResponse:
        raise OSError(f"no harness status endpoint configured for {url}")

    def post(
        self, url: str, headers: Mapping[str, str], payload: Mapping[str, object]
    ) -> HarnessApiResponse:
        raise OSError(f"no harness status endpoint configured for {url}")


def _portfolio_document(summary: PortfolioSummary) -> dict[str, object]:
    """Map the inert status record onto the published portfolio shape."""
    return {
        "cash": summary.cash,
        "equity": summary.equity,
        "books": [
            {
                "name": book.name,
                "cash": book.cash,
                "equity": book.equity,
                "positions": [
                    {
                        "symbol": position.symbol,
                        "quantity": position.quantity,
                        "average_price": position.average_price,
                    }
                    for position in book.positions
                ],
            }
            for book in summary.books
        ],
    }


def _risk_decision_document(
    decisions: tuple[RiskDecisionRecord, ...],
) -> list[dict[str, object]]:
    return [
        {"rule": item.rule, "decision": item.decision, "reason": item.reason}
        for item in decisions
    ]


class DeploymentDataService:
    """Deployment reads over local records and harness-published surfaces."""

    def __init__(
        self,
        store: DeploymentStore | None = None,
        *,
        status_client: RuntimeStatusClient | None = None,
    ) -> None:
        self._store = store if store is not None else InMemoryDeploymentStore()
        self._status_client = status_client

    @property
    def store(self) -> DeploymentStore:
        return self._store

    @property
    def status_client(self) -> RuntimeStatusClient | None:
        return self._status_client

    def list_deployments(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        return self._store.list_deployments(workspace_id)

    def list_orders(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        return self._store.list_orders(workspace_id)

    def portfolio(self) -> dict[str, object]:
        """Return the published portfolio snapshot or raise for unavailability."""
        return self._consume(
            lambda client: client.portfolio_summary(), _portfolio_document
        )

    def risk_decisions(self, request_ref: str) -> dict[str, object]:
        """Return the audited risk decisions for one request reference."""
        return self._consume(
            lambda client: client.risk_decisions(request_ref),
            _risk_decision_document,
        )

    def _consume[T](
        self,
        fetch: Callable[[RuntimeStatusClient], StatusSurface[T]],
        project: Callable[[T], object],
    ) -> dict[str, object]:
        client = self._status_client
        if client is None:
            raise HarnessStatusError("no harness status endpoint configured")
        try:
            surface = fetch(client)
        except HarnessStatusError:
            raise
        except Exception as error:
            raise HarnessStatusError(
                "the harness status surface could not be reached"
            ) from error
        if surface.state == "unavailable" or surface.value is None:
            raise SurfaceUnavailable(surface.reason or "unavailable")
        return {"state": surface.state, "value": project(surface.value)}


class SurfaceUnavailable(RuntimeError):
    """A harness-published surface is explicitly absent."""


def get_deployment_service(request: Request) -> DeploymentDataService:
    """Resolve the service from app state so applications control persistence."""
    from thesis.http.errors import require_service

    return require_service(request, "deployment_service", DeploymentDataService)


def _service_error(error: Exception) -> HTTPException:
    from thesis.http.errors import service_error

    return service_error(error)


def create_deployment_router() -> APIRouter:
    """Create the deployment router; services resolve from app state."""
    router = APIRouter(prefix="/v1/workspaces/{workspace_id}", tags=["deployments"])

    @router.get("/deployments")
    def list_deployments(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        service: Annotated[DeploymentDataService, Depends(get_deployment_service)],
    ) -> dict[str, object]:
        return {"items": list(service.list_deployments(workspace_id))}

    @router.get("/deployments/orders")
    def list_orders(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        service: Annotated[DeploymentDataService, Depends(get_deployment_service)],
    ) -> dict[str, object]:
        return {"items": list(service.list_orders(workspace_id))}

    @router.get("/deployments/portfolio")
    def get_portfolio(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        service: Annotated[DeploymentDataService, Depends(get_deployment_service)],
    ) -> dict[str, object]:
        del workspace_id
        try:
            return service.portfolio()
        except Exception as error:
            raise _service_error(error) from error

    @router.get("/deployments/risk-decisions")
    def get_risk_decisions(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        request_ref: Annotated[str, Query(min_length=1, max_length=128)],
        service: Annotated[DeploymentDataService, Depends(get_deployment_service)],
    ) -> dict[str, object]:
        del workspace_id
        try:
            return service.risk_decisions(request_ref)
        except Exception as error:
            raise _service_error(error) from error

    return router


__all__ = [
    "DeploymentDataService",
    "DeploymentStore",
    "InMemoryDeploymentStore",
    "SurfaceUnavailable",
    "create_deployment_router",
    "get_deployment_service",
]
