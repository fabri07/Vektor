from abc import ABC, abstractmethod
from typing import Any

from app.application.agents.shared.heuristic_engine import HeuristicConfig
from app.application.agents.shared.schemas import AgentRequest, AgentResponse
from app.application.security.prompt_defense import wrap_user_input as _defense_wrap


class BaseAgent(ABC):

    @property
    @abstractmethod
    def agent_name(self) -> str: ...

    @abstractmethod
    async def process(
        self,
        request: AgentRequest,
        task: Any | None = None,  # AgentTask — opcional, para dispatch task-aware
    ) -> AgentResponse: ...

    def build_system_prompt(self, business: dict, heuristics: HeuristicConfig) -> str:
        """Construye el system prompt inyectando heurísticas como valores numéricos."""
        return heuristics.to_prompt_fragment()

    def wrap_user_input(self, message: str) -> str:
        return _defense_wrap(message)
