from app.application.agents.base import BaseAgent
from app.application.agents.shared.schemas import AgentRequest, AgentResponse


class AgentStock(BaseAgent):
    agent_name = "agent_stock"

    def process(self, request: AgentRequest) -> AgentResponse:
        raise NotImplementedError("AgentStock.process — implementación pendiente")
