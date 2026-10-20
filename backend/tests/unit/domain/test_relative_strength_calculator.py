import random

import pytest

from app.domain.relative_strength import (
    BALANCED_RS_FORMULA_VERSION,
    HORIZON_WEIGHTS,
    HORIZONS,
    StockRsPlacement,
    calculate_balanced_rs,
    percentile_ratings,
    place_stock_rs,
)


def test_percentile_ratings_use_average_ties_and_half_up_mapping():
    assert percentile_ratings({"A": 0.0, "B": 10.0, "C": 10.0, "D": 20.0}) == {
        "A": 1,
        "B": 50,
        "C": 50,
        "D": 99,
    }


def test_percentile_ratings_map_an_all_tied_distribution_to_50():
    assert percentile_ratings({"A": 7.0, "B": 7.0, "C": 7.0}) == {
        "A": 50,
        "B": 50,
        "C": 50,
    }


def test_balanced_weights_are_exactly_one():
    assert sum(HORIZON_WEIGHTS.values()) == pytest.approx(1.0)
    assert HORIZON_WEIGHTS == {
        "1m": 0.20,
        "3m": 0.30,
        "6m": 0.20,
        "9m": 0.15,
        "12m": 0.15,
    }


def test_short_horizon_ratings_are_display_only_and_do_not_change_overall_rs():
    returns = {
        "A": {
            "1d": 1.00,
            "1w": 1.00,
            "1m": -0.40,
            "3m": -0.20,
            "6m": 0.10,
            "9m": 0.40,
            "12m": 10.0,
        },
        "B": {
            "1d": 0.10,
            "1w": 0.10,
            "1m": 0.10,
            "3m": 0.15,
            "6m": 0.20,
            "9m": 0.30,
            "12m": 1.0,
        },
        "C": {
            "1d": -0.10,
            "1w": -0.10,
            "1m": 0.20,
            "3m": 0.25,
            "6m": 0.30,
            "9m": 0.20,
            "12m": 0.5,
        },
    }
    baseline = calculate_balanced_rs(returns)
    flipped_short_term = calculate_balanced_rs(
        {
            **returns,
            "A": {**returns["A"], "1d": -10.0, "1w": -10.0},
            "C": {**returns["C"], "1d": 10.0, "1w": 10.0},
        }
    )

    assert baseline["A"].rs_1d == 99
    assert baseline["A"].rs_1w == 99
    assert flipped_short_term["A"].rs_1d == 1
    assert flipped_short_term["A"].rs_1w == 1
    assert (
        flipped_short_term["A"].weighted_composite == baseline["A"].weighted_composite
    )
    assert flipped_short_term["A"].overall_rs == baseline["A"].overall_rs


def test_raw_magnitude_cannot_change_a_symbol_after_horizon_rank_is_fixed():
    returns = {
        "A": {
            "1d": 0.0,
            "1w": 0.0,
            "1m": -0.40,
            "3m": -0.20,
            "6m": 0.10,
            "9m": 0.40,
            "12m": 10.0,
        },
        "B": {
            "1d": 0.0,
            "1w": 0.0,
            "1m": 0.10,
            "3m": 0.15,
            "6m": 0.20,
            "9m": 0.30,
            "12m": 1.0,
        },
        "C": {
            "1d": 0.0,
            "1w": 0.0,
            "1m": 0.20,
            "3m": 0.25,
            "6m": 0.30,
            "9m": 0.20,
            "12m": 0.5,
        },
    }
    baseline = calculate_balanced_rs(returns)
    extreme = calculate_balanced_rs({**returns, "A": {**returns["A"], "12m": 10_000.0}})

    assert baseline["A"].rs_12m == 99
    assert extreme["A"].rs_12m == 99
    assert extreme["A"].weighted_composite == baseline["A"].weighted_composite
    assert extreme["A"].overall_rs == baseline["A"].overall_rs


def test_recent_relative_weakness_controls_half_of_the_composite():
    scores = calculate_balanced_rs(
        {
            "FORMER": {
                "1d": 0.0,
                "1w": 0.0,
                "1m": -0.50,
                "3m": -0.35,
                "6m": 0.10,
                "9m": 1.0,
                "12m": 10.0,
            },
            "STEADY": {
                "1d": 0.0,
                "1w": 0.0,
                "1m": 0.12,
                "3m": 0.18,
                "6m": 0.20,
                "9m": 0.25,
                "12m": 0.30,
            },
            "MIDDLE": {
                "1d": 0.0,
                "1w": 0.0,
                "1m": 0.02,
                "3m": 0.04,
                "6m": 0.08,
                "9m": 0.12,
                "12m": 0.15,
            },
        }
    )

    assert scores["FORMER"].rs_1m == 1
    assert scores["FORMER"].rs_3m == 1
    assert scores["FORMER"].weighted_composite < scores["STEADY"].weighted_composite


def test_calculator_requires_one_common_complete_eligible_set():
    with pytest.raises(ValueError, match="missing horizons"):
        calculate_balanced_rs(
            {
                "A": {
                    "1d": 0.0,
                    "1w": 0.0,
                    "1m": 0.1,
                    "3m": 0.2,
                    "6m": 0.3,
                    "9m": 0.4,
                    "12m": 0.5,
                },
                "B": {"1m": 0.1, "3m": 0.2},
            }
        )


def test_formula_version_is_stable():
    assert BALANCED_RS_FORMULA_VERSION == "balanced-horizon-percentile-v2"


# --- young-stock placement -------------------------------------------------

E3 = {
    symbol: {horizon: value for horizon in HORIZONS}
    for symbol, value in (("A", -0.1), ("B", 0.0), ("C", 0.1))
}
E4 = {
    symbol: {horizon: value for horizon in HORIZONS}
    for symbol, value in (("A", 0.0), ("B", 10.0), ("C", 10.0), ("D", 20.0))
}
YNG = {"1d": 0.2, "1w": 0.2, "1m": 0.05, "3m": -0.2}


def test_young_stock_is_placed_on_its_own_horizons_against_the_eligible_set():
    placed = place_stock_rs({"YNG": YNG}, against=E3)

    assert placed == {
        "YNG": StockRsPlacement(
            "YNG",
            ratings={"1d": 99, "1w": 99, "1m": 66, "3m": 1},
            excess_returns=YNG,
        )
    }
    assert placed["YNG"].scanner_fields() == {"rs_rating_1m": 66, "rs_rating_3m": 1}


@pytest.mark.parametrize(
    ("value", "expected"),
    [(-5.0, 1), (0.0, 13), (5.0, 26), (10.0, 50), (15.0, 75), (20.0, 87), (25.0, 99)],
)
def test_placement_equals_percentile_ratings_with_the_stock_added(value, expected):
    placed = place_stock_rs({"Y": {"1d": value}}, against=E4)["Y"].ratings["1d"]

    assert placed == expected
    assert placed == percentile_ratings(
        {"A": 0.0, "B": 10.0, "C": 10.0, "D": 20.0, "Y": value}
    )["Y"]


def test_placement_matches_percentile_ratings_over_random_tied_sets():
    rng = random.Random(20261021)
    for _ in range(500):
        eligible = {f"S{i}": float(rng.randrange(8)) for i in range(rng.randint(2, 30))}
        value = float(rng.randrange(8))
        against = {s: {h: v for h in HORIZONS} for s, v in eligible.items()}

        placed = place_stock_rs({"Y": {"1d": value}}, against=against)["Y"]

        assert placed.ratings["1d"] == percentile_ratings({**eligible, "Y": value})["Y"]


def test_leave_one_out_placement_reproduces_the_stored_rating_on_each_horizon():
    eligible = {
        "A": {"1d": 0.0, "1w": 3.0, "1m": 1.0, "3m": 9.0, "6m": 2.0, "9m": 5.0, "12m": 7.0},
        "B": {"1d": 10.0, "1w": 3.0, "1m": 0.0, "3m": 1.0, "6m": 2.0, "9m": 4.0, "12m": 1.0},
        "C": {"1d": 10.0, "1w": 1.0, "1m": 5.0, "3m": 4.0, "6m": 2.0, "9m": 4.0, "12m": 1.0},
        "D": {"1d": 20.0, "1w": 0.0, "1m": 5.0, "3m": 4.0, "6m": 7.0, "9m": 1.0, "12m": 3.0},
    }
    scores = calculate_balanced_rs(eligible)
    assert {s: scores[s].rs_1d for s in eligible} == {"A": 1, "B": 50, "C": 50, "D": 99}
    assert {s: scores[s].rs_9m for s in eligible} == {"A": 99, "B": 50, "C": 50, "D": 1}

    for symbol in eligible:
        others = {s: v for s, v in eligible.items() if s != symbol}
        for index, horizon in enumerate(HORIZONS[:-1]):
            prefix = {h: eligible[symbol][h] for h in HORIZONS[: index + 1]}

            placed = place_stock_rs({symbol: prefix}, against=others)[symbol]

            assert placed.ratings[horizon] == getattr(scores[symbol], f"rs_{horizon}"), (
                symbol,
                horizon,
            )


def test_young_stocks_never_move_each_other():
    alone = place_stock_rs({"YNG": YNG}, against=E3)["YNG"].ratings

    together = place_stock_rs({"YNG": YNG, "Z": {"1d": 0.15}}, against=E3)

    assert together["YNG"].ratings == alone == {"1d": 99, "1w": 99, "1m": 66, "3m": 1}
    assert together["Z"].ratings == {"1d": 99}
    assert together["Z"].scanner_fields() == {}


@pytest.mark.parametrize(
    ("ratings", "returns"),
    [
        ({"1d": 50, "1m": 50}, {"1d": 0.0, "1m": 0.0}),
        ({h: 50 for h in HORIZONS}, {h: 0.0 for h in HORIZONS}),
        ({}, {}),
        ({"1d": 0}, {"1d": 0.0}),
        ({"1d": 100}, {"1d": 0.0}),
        ({"1d": 50}, {"1d": float("nan")}),
        ({"1d": 50}, {"1d": 0.0, "1w": 0.0}),
    ],
)
def test_placement_shape_is_enforced(ratings, returns):
    with pytest.raises(ValueError):
        StockRsPlacement("X", ratings, returns)


def test_place_rejects_a_symbol_that_is_both_young_and_eligible():
    with pytest.raises(ValueError, match="both eligible and young"):
        place_stock_rs({"A": {"1d": 0.0}}, against=E3)


def test_place_requires_two_eligible_stocks():
    with pytest.raises(ValueError, match="at least two"):
        place_stock_rs({"Y": {"1d": 0.0}}, against={"A": E3["A"]})


def test_percentile_ratings_map_five_distinct_values_to_the_fixed_scale():
    assert percentile_ratings({"A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0, "E": 5.0}) == {
        "A": 1,
        "B": 26,
        "C": 50,
        "D": 75,
        "E": 99,
    }


def test_place_rejects_horizons_that_are_not_a_leading_prefix():
    with pytest.raises(ValueError, match=r"Y excess returns \['1d', '1w', '3m'\] are not a leading prefix"):
        place_stock_rs({"Y": {"1d": 0.1, "1w": 0.2, "3m": 0.3}}, against=E3)
