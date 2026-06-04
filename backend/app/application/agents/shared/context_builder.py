CONTEXT_BUDGETS = {
    "agent_ceo": 2000,
    # Stage 2: nuevos agentes
    "agent_income": 3000,
    "agent_expense": 3000,
    "agent_google": 3500,  # combina budget de calendar + sync
    # Agentes existentes
    "agent_stock": 3000,
    "agent_supplier": 3500,
    "agent_health": 4000,
    "agent_helper": 2500,
    # Alias deprecado (Stage 2a)
    "agent_cash": 3000,
}

CONTEXT_PRIORITY = [
    ("intent_and_entities", 200),
    ("business_heuristics", 300),
    ("agent_memory", 300),
    ("uploaded_files", 400),
    ("current_snapshot", 600),
    ("recent_events", 800),
    ("conversation_history", 1000),
    ("historical_data", 400),
]


class ContextBuilder:
    def __init__(self, agent_name: str) -> None:
        self.budget = CONTEXT_BUDGETS.get(agent_name, 2000)
        self.parts: dict[str, str] = {}

    def add(self, key: str, content: str) -> "ContextBuilder":
        self.parts[key] = content
        return self

    def build(self) -> str:
        used = 0
        selected = []
        for key, cost in CONTEXT_PRIORITY:
            if key in self.parts and used + cost <= self.budget:
                selected.append(self.parts[key])
                used += cost
        return "\n---\n".join(selected)
