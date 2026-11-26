"""Executable asset: derives handler names from contract endpoints (behaviour-tested at publish)."""
import re


def run(inp):
    names = [re.sub(r"(?<!^)(?=[A-Z])", "_", e["operation_id"]).lower() for e in inp.get("endpoints", [])]
    return {"handlers": names, "count": len(names)}
