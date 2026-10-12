from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import CashMovement, CashMovementType
from app.models.cash_movement import CASH_OUTFLOW_TYPES


def _signed_amount():
    """Las salidas y los retiros restan; las entradas y los aportes suman."""
    return case(
        (
            CashMovement.type.in_(list(CASH_OUTFLOW_TYPES)),
            -CashMovement.amount,
        ),
        else_=CashMovement.amount,
    )


class CashMovementRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, movement: CashMovement) -> CashMovement:
        self.db.add(movement)
        self.db.flush()
        self.db.refresh(movement)
        return movement

    def get_all(
        self,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[CashMovement]:
        stmt = select(CashMovement)
        if start is not None:
            stmt = stmt.where(CashMovement.occurred_at >= start)
        if end is not None:
            stmt = stmt.where(CashMovement.occurred_at <= end)
        # created_at e id desempatan.  Con occurred_at los empates son raros,
        # pero una carga en lote los produce, y sin desempate el orden queda
        # indefinido y el saldo acumulado deja de ser reproducible.
        stmt = (
            stmt.order_by(
                CashMovement.occurred_at, CashMovement.created_at, CashMovement.id
            )
            .limit(limit)
            .offset(offset)
        )
        return list(self.db.execute(stmt).scalars().all())

    def _range_filters(self, stmt, start: Optional[datetime], end: Optional[datetime]):
        if start is not None:
            stmt = stmt.where(CashMovement.occurred_at >= start)
        if end is not None:
            stmt = stmt.where(CashMovement.occurred_at <= end)
        return stmt

    def count(
        self, start: Optional[datetime] = None, end: Optional[datetime] = None
    ) -> int:
        stmt = self._range_filters(
            select(func.count()).select_from(CashMovement), start, end
        )
        return self.db.execute(stmt).scalar_one()

    def get_recent_with_balance(
        self,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[tuple[CashMovement, Decimal]]:
        """Movimientos del mas reciente al mas antiguo, cada uno con el saldo
        que dejo en caja.

        El saldo se calcula con una ventana sobre TODO el libro y el rango se
        filtra despues: filtrar antes haria que el saldo arranque en 0 al
        inicio del rango en vez de arrastrar lo anterior.  El orden de la
        ventana es el mismo de `get_all`, para que ambos den el mismo saldo.
        """
        running_balance = (
            func.sum(_signed_amount())
            .over(
                order_by=(
                    CashMovement.occurred_at,
                    CashMovement.created_at,
                    CashMovement.id,
                )
            )
            .label("running_balance")
        )
        ledger = select(
            CashMovement.id.label("movement_id"), running_balance
        ).subquery()
        stmt = select(CashMovement, ledger.c.running_balance).join(
            ledger, ledger.c.movement_id == CashMovement.id
        )
        stmt = self._range_filters(stmt, start, end)
        stmt = (
            stmt.order_by(
                CashMovement.occurred_at.desc(),
                CashMovement.created_at.desc(),
                CashMovement.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        return [
            (m, Decimal(str(balance))) for m, balance in self.db.execute(stmt).all()
        ]

    def get_running_balance(self, as_of: Optional[datetime] = None) -> Decimal:
        stmt = select(func.coalesce(func.sum(_signed_amount()), 0))
        if as_of is not None:
            stmt = stmt.where(CashMovement.occurred_at <= as_of)
        return Decimal(str(self.db.execute(stmt).scalar_one()))

    def get_owner_balance(self) -> Decimal:
        contributed = func.coalesce(
            func.sum(
                case(
                    (
                        CashMovement.type == CashMovementType.APORTE_SOCIO,
                        CashMovement.amount,
                    ),
                    else_=0,
                )
            ),
            0,
        )
        withdrawn = func.coalesce(
            func.sum(
                case(
                    (
                        CashMovement.type == CashMovementType.RETIRO_SOCIO,
                        CashMovement.amount,
                    ),
                    else_=0,
                )
            ),
            0,
        )
        return Decimal(
            str(self.db.execute(select(contributed - withdrawn)).scalar_one())
        )
