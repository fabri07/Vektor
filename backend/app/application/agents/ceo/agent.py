from app.application.agents.base import BaseAgent
from app.application.agents.shared.schemas import AgentRequest, AgentResponse


class AgentCEO(BaseAgent):
    agent_name = "agent_ceo"

    def process(self, request: AgentRequest) -> AgentResponse:
        raise NotImplementedError("AgentCEO.process — implementación pendiente")
