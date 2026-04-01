from app.application.agents.base import BaseAgent
from app.application.agents.shared.schemas import AgentRequest, AgentResponse


class AgentHealth(BaseAgent):
    agent_name = "agent_health"

    async def process(self, request: AgentRequest) -> AgentResponse:
        raise NotImplementedError("AgentHealth.process — implementación pendiente")
