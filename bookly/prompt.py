"""Builds Dewey's system prompt from the markdown files in prompts/. The prompt text lives in
markdown so a CX lead can read and edit it like a playbook; this file only glues it together."""

from functools import cache

from bookly.config import PROMPTS_DIR, TODAY

# Order matters: rules first, then voice, then procedures.
PROMPT_FILES = ["base.md", "dewey.md", "order_status.md"]


@cache  # the prompt never changes during a run, which also keeps it byte-identical for prompt caching
def build_system_prompt() -> str:
    """Join the prompt files into one system prompt, filling in today's date."""
    parts = [(PROMPTS_DIR / name).read_text().strip() for name in PROMPT_FILES]
    return "\n\n".join(parts).replace("{today}", TODAY.isoformat())
