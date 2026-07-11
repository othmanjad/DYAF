"""Expression language for HAVING clauses and computed fields.

One small, safe expression engine (no eval()) used everywhere an analyst
writes logic between values:

* HAVING:          debit_sum > credit_sum AND tx_count >= 3
* computed fields: amount % 100 == 0
* threshold math:  total > 3 * max_declared

Grammar (precedence low -> high):
    or_expr    := and_expr (OR and_expr)*
    and_expr   := not_expr (AND not_expr)*
    not_expr   := NOT not_expr | comparison
    comparison := additive ((= | == | != | <> | > | >= | < | <=) additive)?
    additive   := term ((+|-) term)*
    term       := factor ((*|/|%) factor)*
    factor     := NUMBER | STRING | IDENT | ( or_expr ) | - factor

Identifiers resolve against a context dict (aggregate names for HAVING,
document fields for computed fields). The same AST compiles to an
Elasticsearch Painless script for query pushdown previews.
"""
from __future__ import annotations

import re
from typing import Any, Optional

_TOKEN_RE = re.compile(r"""
    \s*(?:
      (?P<num>\d+(?:\.\d+)?)
    | (?P<str>'[^']*')
    | (?P<op><=|>=|==|!=|<>|=|<|>|\+|-|\*|/|%|\(|\))
    | (?P<ident>[A-Za-z_@][A-Za-z0-9_.@-]*)
    )""", re.VERBOSE)

_KEYWORDS = {"and", "or", "not", "true", "false"}


class ExprError(ValueError):
    pass


def tokenize(text: str) -> list[tuple[str, str]]:
    tokens, pos = [], 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m or m.end() == pos:
            rest = text[pos:].strip()
            if not rest:
                break
            raise ExprError(f"Unexpected character at: {rest[:20]!r}")
        pos = m.end()
        if m.group("num") is not None:
            tokens.append(("num", m.group("num")))
        elif m.group("str") is not None:
            tokens.append(("str", m.group("str")[1:-1]))
        elif m.group("op") is not None:
            tokens.append(("op", m.group("op")))
        else:
            ident = m.group("ident")
            low = ident.lower()
            tokens.append(("kw", low) if low in _KEYWORDS else ("ident", ident))
    return tokens


# AST nodes: ("num", 1.0) ("str", s) ("bool", b) ("var", name)
#            ("un", op, a) ("bin", op, a, b)

class Parser:
    def __init__(self, tokens: list[tuple[str, str]]):
        self.tokens = tokens
        self.i = 0

    def peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else (None, None)

    def next(self):
        tok = self.peek()
        self.i += 1
        return tok

    def expect_op(self, op: str):
        kind, val = self.next()
        if kind != "op" or val != op:
            raise ExprError(f"Expected '{op}', got {val!r}")

    def parse(self):
        node = self.or_expr()
        if self.i != len(self.tokens):
            raise ExprError(f"Unexpected token {self.peek()[1]!r}")
        return node

    def or_expr(self):
        node = self.and_expr()
        while self.peek() == ("kw", "or"):
            self.next()
            node = ("bin", "or", node, self.and_expr())
        return node

    def and_expr(self):
        node = self.not_expr()
        while self.peek() == ("kw", "and"):
            self.next()
            node = ("bin", "and", node, self.not_expr())
        return node

    def not_expr(self):
        if self.peek() == ("kw", "not"):
            self.next()
            return ("un", "not", self.not_expr())
        return self.comparison()

    def comparison(self):
        node = self.additive()
        kind, val = self.peek()
        if kind == "op" and val in ("=", "==", "!=", "<>", ">", ">=", "<", "<="):
            self.next()
            op = {"=": "==", "<>": "!="}.get(val, val)
            node = ("bin", op, node, self.additive())
        return node

    def additive(self):
        node = self.term()
        while self.peek()[0] == "op" and self.peek()[1] in ("+", "-"):
            op = self.next()[1]
            node = ("bin", op, node, self.term())
        return node

    def term(self):
        node = self.factor()
        while self.peek()[0] == "op" and self.peek()[1] in ("*", "/", "%"):
            op = self.next()[1]
            node = ("bin", op, node, self.factor())
        return node

    def factor(self):
        kind, val = self.next()
        if kind == "num":
            return ("num", float(val))
        if kind == "str":
            return ("str", val)
        if kind == "kw" and val in ("true", "false"):
            return ("bool", val == "true")
        if kind == "ident":
            return ("var", val)
        if kind == "op" and val == "(":
            node = self.or_expr()
            self.expect_op(")")
            return node
        if kind == "op" and val == "-":
            return ("un", "neg", self.factor())
        raise ExprError(f"Unexpected token {val!r}")


def parse(text: str):
    """Parse an expression into an AST; raises ExprError on bad input."""
    if not text or not text.strip():
        raise ExprError("Empty expression")
    return Parser(tokenize(text)).parse()


def variables(node) -> set[str]:
    """All identifiers referenced by the expression."""
    kind = node[0]
    if kind == "var":
        return {node[1]}
    if kind == "un":
        return variables(node[2])
    if kind == "bin":
        return variables(node[2]) | variables(node[3])
    return set()


def _num(v) -> float:
    if v is None:
        return 0.0
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def evaluate(node, context: dict) -> Any:
    """Evaluate the AST against a context dict (missing vars -> None/0)."""
    kind = node[0]
    if kind == "num":
        return node[1]
    if kind == "str":
        return node[1]
    if kind == "bool":
        return node[1]
    if kind == "var":
        return context.get(node[1])
    if kind == "un":
        val = evaluate(node[2], context)
        return (not _truthy(val)) if node[1] == "not" else -_num(val)
    op, a, b = node[1], evaluate(node[2], context), evaluate(node[3], context)
    if op == "and":
        return _truthy(a) and _truthy(b)
    if op == "or":
        return _truthy(a) or _truthy(b)
    if op in ("==", "!="):
        if isinstance(a, str) or isinstance(b, str):
            eq = (a is not None and b is not None and str(a) == str(b))
        else:
            eq = _num(a) == _num(b)
        return eq if op == "==" else not eq
    if op in (">", ">=", "<", "<="):
        na, nb = _num(a), _num(b)
        return {">": na > nb, ">=": na >= nb, "<": na < nb, "<=": na <= nb}[op]
    na, nb = _num(a), _num(b)
    if op == "+":
        return na + nb
    if op == "-":
        return na - nb
    if op == "*":
        return na * nb
    if op == "/":
        return na / nb if nb != 0 else 0.0
    if op == "%":
        return na % nb if nb != 0 else 0.0
    raise ExprError(f"Unknown operator {op}")


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return v != 0
    return bool(v)


def to_painless(node, var_render=None) -> str:
    """Compile the AST to an Elasticsearch Painless expression.

    var_render(name) -> painless reference; defaults to params.<name>
    (the convention used by bucket_selector / bucket_script).
    """
    render = var_render or (lambda name: f"params.{name}")
    kind = node[0]
    if kind == "num":
        return repr(node[1])
    if kind == "str":
        return "'" + node[1].replace("'", "\\'") + "'"
    if kind == "bool":
        return "true" if node[1] else "false"
    if kind == "var":
        return render(node[1])
    if kind == "un":
        inner = to_painless(node[2], var_render)
        return f"!({inner})" if node[1] == "not" else f"-({inner})"
    op, a, b = node[1], to_painless(node[2], var_render), to_painless(node[3], var_render)
    ops = {"and": "&&", "or": "||", "==": "==", "!=": "!=",
           ">": ">", ">=": ">=", "<": "<", "<=": "<=",
           "+": "+", "-": "-", "*": "*", "/": "/", "%": "%"}
    return f"({a} {ops[op]} {b})"
