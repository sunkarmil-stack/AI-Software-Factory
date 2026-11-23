"""Safe business-rule expressions.

Rules are written in a tiny, side-effect-free expression language (a whitelisted subset of
Python expression syntax), e.g.  `amount_cents >= 1 and amount_cents <= 100000000`
or `status in ['open', 'review'] and len(reason) > 3`.
No attribute access, subscripts on objects, imports, lambdas or arbitrary calls are allowed,
so a rule written by a person or proposed by an agent can never execute code.
"""
from __future__ import annotations

import ast
import operator

FUNCS = {"len": len, "lower": lambda s: str(s).lower(), "upper": lambda s: str(s).upper(),
         "abs": abs, "min": min, "max": max}
BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
       ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv}
CMP = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt,
       ast.GtE: operator.ge, ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b}
ALLOWED = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.BinOp, ast.Compare,
           ast.Name, ast.Load, ast.Constant, ast.List, ast.Tuple, ast.Call, ast.IfExp, *BIN, *CMP)


class RuleSyntaxError(ValueError):
    pass


class UnknownValue(LookupError):
    """A name the rule needs is not present in the data: the rule cannot be decided."""


def parse(expr: str) -> ast.Expression:
    if not isinstance(expr, str) or not expr.strip() or len(expr) > 500:
        raise RuleSyntaxError("expression must be a non-empty string of at most 500 characters")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise RuleSyntaxError(f"syntax error: {e.msg}")
    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED):
            raise RuleSyntaxError(f"'{type(node).__name__}' is not allowed in rule expressions")
        if isinstance(node, ast.Call) and (not isinstance(node.func, ast.Name) or node.func.id not in FUNCS or node.keywords):
            raise RuleSyntaxError(f"only these functions are allowed: {sorted(FUNCS)}")
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise RuleSyntaxError("names may not start with an underscore")
    return tree


def names(expr: str) -> set[str]:
    tree = parse(expr)
    called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call)}
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} - called - {"True", "False", "None"}


def evaluate(expr: str, data: dict) -> bool:
    tree = parse(expr)

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.Name):
            if n.id in ("True", "False", "None"):
                return {"True": True, "False": False, "None": None}[n.id]
            if n.id not in data or data[n.id] is None:
                raise UnknownValue(n.id)
            return data[n.id]
        if isinstance(n, (ast.List, ast.Tuple)):
            return [ev(e) for e in n.elts]
        if isinstance(n, ast.BoolOp):
            if isinstance(n.op, ast.And):
                return all(ev(v) for v in n.values)
            return any(ev(v) for v in n.values)
        if isinstance(n, ast.UnaryOp):
            v = ev(n.operand)
            return (not v) if isinstance(n.op, ast.Not) else -v
        if isinstance(n, ast.BinOp):
            return BIN[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.Compare):
            left = ev(n.left)
            for op, right in zip(n.ops, n.comparators):
                r = ev(right)
                if not CMP[type(op)](left, r):
                    return False
                left = r
            return True
        if isinstance(n, ast.IfExp):
            return ev(n.body) if ev(n.test) else ev(n.orelse)
        if isinstance(n, ast.Call):
            return FUNCS[n.func.id](*[ev(a) for a in n.args])
        raise RuleSyntaxError(type(n).__name__)

    try:
        return bool(ev(tree))
    except UnknownValue:
        raise
    except (TypeError, ValueError, ZeroDivisionError) as e:
        raise RuleSyntaxError(f"rule could not be evaluated: {e}")
