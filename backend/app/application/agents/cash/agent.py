from app.application.agents.base import BaseAgent
from app.application.agents.shared.schemas import AgentRequest, AgentResponse


class AgentCash(BaseAgent):
    agent_name = "agent_cash"

    def process(self, request: AgentRequest) -> AgentResponse:
        raise NotImplementedError("AgentCash.process — implementación pendiente")
