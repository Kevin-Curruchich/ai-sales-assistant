import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import CashMovement, CashMovementType


def _signed_amount():
    """Las salidas y los retiros restan; las entradas y los aportes suman."""
    return case(
        (
            CashMovement.type.in_(
                [CashMovementType.SALIDA, CashMovementType.RETIRO_SOCIO]
            ),
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
        stmt = stmt.order_by(
            CashMovement.occurred_at, CashMovement.created_at, CashMovement.id
        ).limit(limit).offset(offset)
        return list(self.db.execute(stmt).scalars().all())

    def get_running_balance(self, as_of: Optional[datetime] = None) -> Decimal:
        stmt = select(func.coalesce(func.sum(_signed_amount()), 0))
        if as_of is not None:
            stmt = stmt.where(CashMovement.occurred_at <= as_of)
        return Decimal(str(self.db.execute(stmt).scalar_one()))

    def get_owner_balance(self) -> Decimal:
        contributed = func.coalesce(
            func.sum(
                case(
                    (CashMovement.type == CashMovementType.APORTE_SOCIO, CashMovement.amount),
                    else_=0,
                )
            ),
            0,
        )
        withdrawn = func.coalesce(
            func.sum(
                case(
                    (CashMovement.type == CashMovementType.RETIRO_SOCIO, CashMovement.amount),
                    else_=0,
                )
            ),
            0,
        )
        return Decimal(str(self.db.execute(select(contributed - withdrawn)).scalar_one()))
