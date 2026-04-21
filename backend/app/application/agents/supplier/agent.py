"""AgentSupplier — gestión básica de consultas de proveedores sin integraciones externas."""

from __future__ import annotations

from app.application.agents.base import BaseAgent
from app.application.agents.shared.schemas import AgentRequest, AgentResponse, Confidence, RiskLevel


class AgentSupplier(BaseAgent):
    agent_name = "agent_supplier"

    def __init__(self, session=None) -> None:
        self._session = session

    async def process(self, request: AgentRequest) -> AgentResponse:
        message = request.message.lower()

        if "proveedor" in message or "compra" in message:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.HIGH,
                question=(
                    "Actualmente no tenemos conexión automática a Gmail/Calendar/Drive. "
                    "Si querés, indicame proveedor, monto y fecha para registrar la compra manualmente."
                ),
                result={"summary": "Gestión de proveedores en modo manual."},
            )

        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="success",
            risk_level=RiskLevel.LOW,
            confidence=Confidence.HIGH,
            result={"summary": "No detecté una acción concreta de proveedores."},
        )
