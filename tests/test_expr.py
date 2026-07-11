import pytest

from dyaf import expr


def ev(text, **ctx):
    return expr.evaluate(expr.parse(text), ctx)


def test_arithmetic_and_precedence():
    assert ev("1 + 2 * 3") == 7
    assert ev("(1 + 2) * 3") == 9
    assert ev("10 / 4") == 2.5
    assert ev("10 % 3") == 1
    assert ev("-5 + 2") == -3


def test_comparisons_and_logic():
    assert ev("a > b", a=3, b=2) is True
    assert ev("a >= 3 AND b < 1", a=3, b=0) is True
    assert ev("a == 1 OR b == 2", a=9, b=2) is True
    assert ev("NOT (a > 0)", a=1) is False
    assert ev("a != b", a=1, b=2) is True
    assert ev("a = b", a=2, b=2) is True   # SQL-style equals
    assert ev("a <> b", a=2, b=2) is False


def test_aggregate_style_expressions():
    ctx = {"debit_sum": 1000, "credit_sum": 400, "cnt": 5, "declared": 200}
    assert ev("debit_sum > credit_sum", **ctx) is True
    assert ev("debit_sum >= 0.8 * credit_sum AND cnt >= 3", **ctx) is True
    assert ev("debit_sum > 3 * declared", **ctx) is True
    assert ev("cnt >= 1 AND credit_sum == 0", **ctx) is False


def test_division_by_zero_is_safe():
    assert ev("a / b", a=10, b=0) == 0.0
    assert ev("a % b", a=10, b=0) == 0.0


def test_missing_variables_treated_as_zero_like():
    assert ev("missing_agg > 5") is False
    assert ev("missing_agg == 0") is True


def test_strings():
    assert ev("s == 'debit'", s="debit") is True
    assert ev("s != 'credit'", s="debit") is True


def test_variables_listing():
    ast = expr.parse("a > 2 * b AND (c == 0 OR a < 5)")
    assert expr.variables(ast) == {"a", "b", "c"}


def test_parse_errors():
    for bad in ("", "a >", "1 +", "((a)", "a ? b"):
        with pytest.raises(expr.ExprError):
            expr.parse(bad)


def test_painless_compilation():
    ast = expr.parse("recent >= 1 AND prior == 0")
    assert expr.to_painless(ast) == "((params.recent >= 1.0) && (params.prior == 0.0))"
    ast = expr.parse("amount % 1000 == 0")
    painless = expr.to_painless(ast, var_render=lambda f: f"doc['{f}'].value")
    assert painless == "((doc['amount'].value % 1000.0) == 0.0)"
