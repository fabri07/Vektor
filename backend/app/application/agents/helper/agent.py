from app.application.agents.base import BaseAgent
from app.application.agents.shared.schemas import AgentRequest, AgentResponse


class AgentHelper(BaseAgent):
    agent_name = "agent_helper"

    async def process(self, request: AgentRequest) -> AgentResponse:
        raise NotImplementedError("AgentHelper.process — implementación pendiente")
