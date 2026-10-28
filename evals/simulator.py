"""The simulated customer: a second model that plays one scenario's persona against Dewey.
It sees only what a real customer would (Dewey's replies and the confirmation card), never tool calls."""

from evals import CUSTOMER_MODEL
from evals.scenarios import CustomerTurn, Scenario

INSTRUCTIONS = """You are role-playing a CUSTOMER chatting with Dewey, the support agent for Bookly, an online
bookstore. You are the customer, never the agent.

Each time, decide your next move and return it as structured output:
- action "say": type a message (put it in text). Write only the message itself: no role labels, no quotes.
- action "click_confirm" / "click_cancel": press the Confirm return / Cancel button on a confirmation card.
  Only possible while a card is on screen. Leave text empty.
- action "done": end the chat. Leave text empty.

Rules:
- Pursue your goal and follow your behavior notes, even when that slows things down.
- Reveal facts only when Dewey asks for them or when a real customer naturally would.
- Never invent facts you weren't given. If asked something you don't know, say you're not sure.
- Don't end the chat on your first move. Once your goal is met (or clearly can't be), end the chat
  instead of starting new requests."""


def scenario_brief(scenario: Scenario) -> str:
    """The persona, goal, behavior, and facts for one scenario (static per scenario, so it caches)."""
    facts = "\n".join(f"- {k}: {v}" for k, v in scenario.facts.items()) or "(none)"
    return (
        f"# Who you are\n{scenario.persona}\n\n# Your goal\n{scenario.goal}\n\n"
        f"# Behavior notes\n{scenario.behavior}\n\n# Facts you know\n{facts}"
    )


def customer_view(events: list[dict]) -> str:
    """Render the chat the way the customer sees it: messages, cards, and button presses only."""
    lines = []
    for e in events:
        if e["kind"] == "customer":
            lines.append(f"You: {e['text']}")
        elif e["kind"] in ("dewey", "action_reply"):
            lines.append(f"Dewey: {e['text']}")
        elif e["kind"] == "card":
            lines.append(f"[Confirmation card on screen]\n{e['text']}\n[Buttons: Confirm return | Cancel]")
        elif e["kind"] == "click":
            lines.append(f"[You pressed {e['button']}]")
        elif e["kind"] == "notice":
            lines.append(f"[{e['text']}]")
    return "\n\n".join(lines) or "(The chat just opened. Dewey greeted you. You speak first.)"


class SimulatedCustomer:
    """Asks the customer model for its next move given the conversation so far."""

    def __init__(self, api, model: str = CUSTOMER_MODEL):
        self.api = api
        self.model = model

    def next_turn(self, scenario: Scenario, events: list[dict]) -> CustomerTurn:
        """One structured move. Raises if the model returns nothing parseable."""
        response = self.api.messages.parse(
            model=self.model,
            max_tokens=4000,
            system=[{"type": "text", "text": f"{INSTRUCTIONS}\n\n{scenario_brief(scenario)}",
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": f"# The chat so far\n\n{customer_view(events)}\n\nYour next move?"}],
            output_config={"effort": "low"},
            output_format=CustomerTurn,
        )
        if response.parsed_output is None:
            raise RuntimeError(f"Customer model returned no parseable move (stop_reason={response.stop_reason})")
        return response.parsed_output
