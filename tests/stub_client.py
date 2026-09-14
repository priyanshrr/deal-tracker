"""A stand-in for the Anthropic client so extraction plumbing is testable offline."""
from types import SimpleNamespace


class StubBlock(SimpleNamespace):
    pass


class StubMessages:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = self.responder(kwargs)
        return SimpleNamespace(
            content=[StubBlock(type="tool_use", name="record_deal", input=payload)],
            usage=SimpleNamespace(input_tokens=1200, output_tokens=180),
            model=kwargs.get("model"),
        )


class StubClient:
    def __init__(self, responder):
        self.messages = StubMessages(responder)
