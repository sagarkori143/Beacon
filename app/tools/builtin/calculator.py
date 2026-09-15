"""Arithmetic evaluation.

Implemented by walking a parsed AST against an allow-list rather than by calling
``eval``. ``eval`` on model output is remote code execution with extra steps:
the input comes from a language model that a user can influence through an
uploaded document, so it is untrusted by construction.
"""

from __future__ import annotations

import ast
import math
import operator
from typing import Any

from pydantic import BaseModel, Field

from app.tools.base import Tool, ToolContext, ToolDefinition, ToolResult

_BINARY_OPS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS: dict[type[ast.unaryop], Any] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_FUNCTIONS: dict[str, Any] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "sqrt": math.sqrt,
    "floor": math.floor,
    "ceil": math.ceil,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "pow": math.pow,
}
_CONSTANTS: dict[str, float] = {"pi": math.pi, "e": math.e}

#: Caps runaway exponentiation: 9**9**9 would otherwise hang the worker.
_MAX_EXPONENT = 1000
_MAX_EXPRESSION_CHARS = 500


class CalculatorArgs(BaseModel):
    expression: str = Field(
        description=(
            "Arithmetic expression, e.g. '3000 * 1.1' or 'round(45000 / 3, 2)'. "
            "Supports + - * / // % ** and abs, round, min, max, sum, sqrt, "
            "floor, ceil, log, exp."
        ),
        max_length=_MAX_EXPRESSION_CHARS,
    )


class CalculatorTool(Tool):
    definition = ToolDefinition(
        name="calculator",
        description=(
            "Evaluate an arithmetic expression exactly. Use for any figure that "
            "must be correct -- totals, taxes, per-night rates, conversions."
        ),
        input_model=CalculatorArgs,
        timeout_s=5.0,
    )

    async def execute(self, args: CalculatorArgs, context: ToolContext) -> ToolResult:
        try:
            tree = ast.parse(args.expression.strip(), mode="eval")
            value = _evaluate(tree.body)
        except SyntaxError:
            return ToolResult.failure(
                "invalid_arguments", f"'{args.expression}' is not a valid expression."
            )
        except ZeroDivisionError:
            return ToolResult.failure("invalid_arguments", "Division by zero.")
        except (ValueError, TypeError, OverflowError) as exc:
            return ToolResult.failure("invalid_arguments", str(exc))

        formatted = f"{value:,.10g}" if isinstance(value, float) else f"{value:,}"
        return ToolResult.success(
            f"{args.expression} = {formatted}",
            data={"expression": args.expression, "result": value},
        )


def _evaluate(node: ast.AST) -> Any:
    """Evaluate one AST node. Anything not explicitly allowed is rejected."""
    match node:
        case ast.Constant(value=value) if isinstance(value, int | float):
            return value
        case ast.BinOp(left=left, op=op, right=right):
            handler = _BINARY_OPS.get(type(op))
            if handler is None:
                raise ValueError(f"Unsupported operator: {type(op).__name__}")
            lhs, rhs = _evaluate(left), _evaluate(right)
            if isinstance(op, ast.Pow) and abs(rhs) > _MAX_EXPONENT:
                raise ValueError(f"Exponent too large (max {_MAX_EXPONENT})")
            return handler(lhs, rhs)
        case ast.UnaryOp(op=op, operand=operand):
            handler = _UNARY_OPS.get(type(op))
            if handler is None:
                raise ValueError(f"Unsupported unary operator: {type(op).__name__}")
            return handler(_evaluate(operand))
        case ast.Call(func=ast.Name(id=name), args=call_args, keywords=[]):
            function = _FUNCTIONS.get(name)
            if function is None:
                raise ValueError(f"Unknown function: {name}")
            return function(*[_evaluate(a) for a in call_args])
        case ast.Name(id=name):
            if name in _CONSTANTS:
                return _CONSTANTS[name]
            raise ValueError(f"Unknown name: {name}")
        case ast.Tuple(elts=elements) | ast.List(elts=elements):
            return [_evaluate(e) for e in elements]
        case _:
            raise ValueError(f"Unsupported expression element: {type(node).__name__}")


TOOL = CalculatorTool()
