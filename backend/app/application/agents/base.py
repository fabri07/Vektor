from abc import ABC, abstractmethod

from app.application.agents.shared.schemas import AgentRequest, AgentResponse
from app.application.agents.shared.heuristic_engine import HeuristicConfig


class BaseAgent(ABC):

    @property
    @abstractmethod
    def agent_name(self) -> str: ...

    @abstractmethod
    def process(self, request: AgentRequest) -> AgentResponse: ...

    def build_system_prompt(self, business: dict, heuristics: HeuristicConfig) -> str:
        """Construye el system prompt inyectando heurísticas como valores numéricos."""
        return heuristics.to_prompt_fragment()

    def wrap_user_input(self, message: str) -> str:
        return f"<user_message>{message}</user_message>"
