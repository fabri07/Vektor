from app.application.agents.base import BaseAgent
from app.application.agents.shared.schemas import AgentRequest, AgentResponse


class AgentSupplier(BaseAgent):
    agent_name = "agent_supplier"

    def process(self, request: AgentRequest) -> AgentResponse:
        raise NotImplementedError("AgentSupplier.process — implementación pendiente")
