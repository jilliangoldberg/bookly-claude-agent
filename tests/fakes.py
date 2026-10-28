"""Scripted stand-ins for the Anthropic client, shared by the loop and eval tests.
Deterministic and free: no network, no API key."""

from types import SimpleNamespace

USAGE = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)


def text_reply(text: str, stop_reason: str = "end_turn", model: str = "claude-sonnet-5-5") -> SimpleNamespace:
    """A fake model response containing only text."""
    return SimpleNamespace(stop_reason=stop_reason, stop_details=None, usage=USAGE, model=model,
                           content=[SimpleNamespace(type="text", text=text)])


def tool_calls(*calls: tuple[str, dict], model: str = "claude-sonnet-5-5") -> SimpleNamespace:
    """A fake model response asking for one or more tools."""
    blocks = [SimpleNamespace(type="tool_use", id=f"call_{i}", name=name, input=args)
              for i, (name, args) in enumerate(calls)]
    return SimpleNamespace(stop_reason="tool_use", stop_details=None, usage=USAGE, model=model, content=blocks)


class FakeClient:
    """Plays back scripted responses (or raises scripted errors) and records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.messages = self  # so the agent can call fake.messages.create(...)

    def create(self, **request):
        self.requests.append({**request, "messages": list(request["messages"])})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response
