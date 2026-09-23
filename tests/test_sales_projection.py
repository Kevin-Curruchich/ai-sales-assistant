from datetime import date
from decimal import Decimal

from app.services.sales.projection import (
    confidence_for,
    ewma_interval,
    project,
)


def test_ewma_matches_the_hand_computed_value_for_cecy():
    """Intervalos reales de Cecy con gas: el hueco inicial de 56 dias ya casi no pesa."""
    result = ewma_interval([4, 56, 17, 21, 19])
    assert result.quantize(Decimal("0.0001")) == Decimal("19.3318")


def test_ewma_matches_the_hand_computed_value_for_tia_lily():
    """Tia Lily se esta desacelerando; la media simple daria 11.8."""
    result = ewma_interval([5, 5, 7, 8, 8, 7, 22, 20, 10, 6, 9, 34])
    assert result.quantize(Decimal("0.01")) == Decimal("17.36")


def test_ewma_weights_the_most_recent_observation_most():
    """Con alpha=0.3, un unico valor final distinto solo puede mover la
    estimacion un 30% hacia el (por definicion: peso_ultimo = alpha, siempre,
    sin importar cuantos valores repetidos lo precedan). [2, 2, 30] vs
    [30, 30, 2] no distingue nada por eso: ambos colapsan al mismo calculo de
    dos pasos (alpha*30 + (1-alpha)*2 y alpha*2 + (1-alpha)*30) y el resultado
    queda igual de "atado" al valor viejo en los dos casos. Para que la
    ponderacion por recencia se note hace falta una tendencia real -- varios
    valores recientes distintos, no uno solo -- asi que la secuencia sube o
    baja de a poco en vez de repetir el mismo numero y saltar al final.
    """
    subiendo = ewma_interval([2, 5, 10, 20, 30])
    bajando = ewma_interval([30, 20, 10, 5, 2])
    assert subiendo > bajando


def test_same_day_purchases_count_as_one_occasion():
    """Tres ventas al mismo cliente el mismo dia son UNA ocasion de compra.

    Sin deduplicar, los intervalos de 0 dias arrastrarian el EWMA a cero y el
    cliente quedaria proyectado para 'hoy' de forma permanente. Con una sola
    fecha distinta no hay historial suficiente, que es la respuesta honesta.
    """
    interval, next_date, _, confidence = project(
        [date(2026, 9, 1), date(2026, 9, 1), date(2026, 9, 1)]
    )
    assert (interval, next_date, confidence) == (None, None, "insufficient")


def test_repeats_within_a_real_history_do_not_shrink_the_interval():
    """Dos ventas el dia 1 y una el 11: el intervalo es 10, no 5."""
    interval, next_date, _, _ = project(
        [date(2026, 9, 1), date(2026, 9, 1), date(2026, 9, 11)]
    )
    assert interval == Decimal("10")
    assert next_date == date(2026, 9, 21)


def test_a_single_purchase_is_not_projected():
    """El AGENTS.md lo dice: pedir un estimado inicial en vez de inventarlo."""
    interval, next_date, method, confidence = project([date(2026, 9, 1)])
    assert interval is None
    assert next_date is None
    assert confidence == "insufficient"


def test_no_purchases_is_also_insufficient():
    interval, next_date, _, confidence = project([])
    assert (interval, next_date, confidence) == (None, None, "insufficient")


def test_method_is_always_ewma_for_now():
    _, _, method, _ = project([date(2026, 9, 1), date(2026, 9, 8)])
    assert method == "ewma"


def test_confidence_thresholds():
    assert confidence_for(1) == "insufficient"
    assert confidence_for(2) == "low"
    assert confidence_for(3) == "low"
    assert confidence_for(4) == "medium"
    assert confidence_for(7) == "medium"
    assert confidence_for(8) == "high"


def test_projection_date_is_the_last_purchase_plus_the_interval():
    interval, next_date, _, _ = project([date(2026, 9, 1), date(2026, 9, 11)])
    assert interval == Decimal("10")
    assert next_date == date(2026, 9, 21)


def test_a_customer_without_a_projection_still_appears_in_follow_ups(
    test_engine, migrated_schema
):
    """El AGENTS.md pide un estimado inicial, no que el cliente desaparezca."""
    import uuid as _uuid
    from datetime import date as _date
    from decimal import Decimal as _Decimal

    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    from app.models import Customer, CustomerProductCycle, Product, User
    from app.services.sales import SaleService
    from tests.conftest import TEST_DATABASE_URL

    engine = create_engine(TEST_DATABASE_URL)

    @event.listens_for(engine, "connect")
    def _sp(dbapi_connection, _record):
        cur = dbapi_connection.cursor()
        cur.execute(f'SET search_path TO "{migrated_schema}"')
        cur.close()
        dbapi_connection.commit()

    session = sessionmaker(bind=engine)()
    try:
        customer = Customer(name="Cliente de una sola compra")
        product = Product(sku=f"SKU-{_uuid.uuid4().hex[:6]}", name="Cilindro de gas")
        session.add_all([customer, product])
        session.flush()
        session.add(
            CustomerProductCycle(
                customer_id=customer.id, product_id=product.id,
                avg_interval_days=None, estimated_next_purchase=None,
                last_purchase_date=_date(2026, 8, 1), last_quantity=_Decimal("1"),
                total_purchases=1, projection_method="ewma",
                projection_confidence="insufficient",
            )
        )
        session.commit()

        follow_ups, total = SaleService(session).get_follow_ups(filter_type="all")
        nombres = [f.customer for f in follow_ups]
        assert "Cliente de una sola compra" in nombres
        encontrado = next(f for f in follow_ups if f.customer == "Cliente de una sola compra")
        assert encontrado.status == "needs_estimate"
    finally:
        session.close()
        engine.dispose()
