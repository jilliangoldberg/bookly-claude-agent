"""Simulated-customer evals for Dewey. Live runs call the API and cost money; see `python -m evals --help`.
Nothing in bookly/ imports this package."""

from bookly.config import MODEL as AGENT_MODEL

CUSTOMER_MODEL = "claude-sonnet-5-5"  # plays the customer; low effort is enough to stay in character
JUDGE_MODEL = "claude-opus-5-5"  # a different model from Dewey, so grading isn't self-grading

# USD per million tokens: input, output, cache read, cache write (5-minute TTL is 1.25x input).
PRICES = {
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
}

__all__ = ["AGENT_MODEL", "CUSTOMER_MODEL", "JUDGE_MODEL", "PRICES"]
