"""Tool registry: the only path from the LLM to the backend. The agent sends TOOL_SCHEMAS to the
model and runs whatever it asks for through dispatch(), which never raises."""

from bookly.models import SessionState, tool_error
from bookly.tools.account import VERIFY_CUSTOMER_SCHEMA, verify_customer
from bookly.tools.orders import GET_ORDER_SCHEMA, LIST_ORDERS_SCHEMA, get_order, list_orders

# name -> (python function, JSON schema the model sees)
REGISTRY = {
    "verify_customer": (verify_customer, VERIFY_CUSTOMER_SCHEMA),
    "list_orders": (list_orders, LIST_ORDERS_SCHEMA),
    "get_order": (get_order, GET_ORDER_SCHEMA),
}

# strict=True makes the API guarantee tool inputs match each schema exactly.
TOOL_SCHEMAS = [{**schema, "strict": True} for _, schema in REGISTRY.values()]


def dispatch(name: str, tool_input: dict, session: SessionState) -> dict:
    """Run one tool by name and return its result dict, turning any crash into a structured error."""
    if name not in REGISTRY:
        return tool_error("UNKNOWN_TOOL", f"There is no tool named {name}.")
    fn, _ = REGISTRY[name]
    try:
        return fn(session, **tool_input)
    except Exception as exc:  # a tool bug must never crash the conversation
        result = tool_error("TOOL_ERROR", "Something went wrong on our side with that lookup.")
        result["debug"] = repr(exc)  # visible in the trace for us; the message above is what Dewey relays
        return result
