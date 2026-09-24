from decimal import Decimal

from app.services.sales.pricing import detect_habitual_margin, margins


def test_margins_separates_real_from_extra():
    """Aurita: cobrado 36, costo 34.17, margen base 3.50."""
    real, extra = margins(
        unit_price=Decimal("36.00"),
        cost_basis=Decimal("34.17"),
        base_margin=Decimal("3.50"),
    )
    assert real == Decimal("1.83")
    assert extra == Decimal("-1.67")


def test_three_consistent_discounted_sales_are_a_pattern():
    """Margenes de 2.50, 2.50 y 2.50 contra un base de 3.50."""
    habitual = detect_habitual_margin(
        observed_margins=[Decimal("2.50"), Decimal("2.50"), Decimal("2.50")],
        base_margin=Decimal("3.50"),
    )
    assert habitual == Decimal("2.50")


def test_margins_within_tolerance_still_count_as_a_pattern():
    """La skill acepta hasta Q2 de diferencia entre ellas."""
    habitual = detect_habitual_margin(
        observed_margins=[Decimal("2.50"), Decimal("1.83"), Decimal("2.50")],
        base_margin=Decimal("3.50"),
    )
    assert habitual is not None


def test_a_one_off_discount_does_not_become_the_pattern():
    """Dos ventas al margen estandar y una rebaja puntual no son un patron."""
    habitual = detect_habitual_margin(
        observed_margins=[Decimal("0.50"), Decimal("3.50"), Decimal("3.50")],
        base_margin=Decimal("3.50"),
    )
    assert habitual is None


def test_margins_equal_to_the_standard_are_not_a_pattern():
    """Consistentes pero iguales al base: no hay nada que sugerir distinto."""
    habitual = detect_habitual_margin(
        observed_margins=[Decimal("3.50"), Decimal("3.50"), Decimal("3.50")],
        base_margin=Decimal("3.50"),
    )
    assert habitual is None


def test_fewer_than_three_sales_is_not_enough_history():
    assert detect_habitual_margin([Decimal("2.50"), Decimal("2.50")], Decimal("3.50")) is None
    assert detect_habitual_margin([], Decimal("3.50")) is None


def test_preview_schema_carries_the_habitual_flag():
    from app.schemas.sale import SaleItemPreview

    assert "is_habitual_price" in SaleItemPreview.model_fields
    assert SaleItemPreview.model_fields["is_habitual_price"].default is False
