from lakemig.inventory.sql_functions import FunctionCall, function_calls


def test_counts_calls_and_their_databricks_rendering() -> None:
    calls = function_calls(
        "SELECT levenshtein_distance(a, b), coalesce(x, 0), coalesce(y, 1), approx_percentile(v, 0.5) FROM t"
    )

    assert calls[FunctionCall("coalesce", "coalesce")] == 2
    assert calls[FunctionCall("levenshtein_distance", "levenshtein")] == 1
    assert calls[FunctionCall("approx_percentile", "percentile_approx")] == 1


def test_unknown_function_has_no_target() -> None:
    calls = function_calls("SELECT my_udf(a) FROM t")

    assert calls == {FunctionCall("my_udf", None): 1}


def test_operators_and_predicates_are_not_calls() -> None:
    calls = function_calls("SELECT a + b, a = b, a IS NULL, CASE WHEN a THEN 1 END FROM t WHERE a IN (1, 2)")

    assert not calls
