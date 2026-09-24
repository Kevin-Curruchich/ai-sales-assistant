import uuid
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.models import CashMovement, CashMovementType
from app.models.cash_movement import CASH_OUTFLOW_TYPES
from app.models.payment_method import PaymentMethod
from app.repositories.cash_movement_repository import CashMovementRepository

MONEY = Decimal("0.01")


class CashService:
    """Libro de caja: dinero que realmente se movio, no ganancia contable.

    El saldo acumulado NO se almacena.  Se calcula al leer.  Guardarlo como
    columna fue la fuente de desincronizacion en la hoja de calculo de la que
    viene este modelo.
    """

    def __init__(self, db: Session):
        self.db = db
        self.repo = CashMovementRepository(db)

    @staticmethod
    def _money(value: Decimal) -> Decimal:
        return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)

    def record(
        self,
        occurred_at: datetime,
        type: CashMovementType,
        amount: Decimal,
        payment_method: Optional[PaymentMethod] = None,
        sale_id: Optional[uuid.UUID] = None,
        purchase_id: Optional[uuid.UUID] = None,
        note: Optional[str] = None,
    ) -> CashMovement:
        amount = self._money(amount)
        if amount <= 0:
            raise ValueError("El monto de un movimiento de caja debe ser mayor a 0")

        movement = CashMovement(
            occurred_at=occurred_at,
            type=type,
            amount=amount,
            payment_method=payment_method,
            sale_id=sale_id,
            purchase_id=purchase_id,
            note=note,
        )
        created = self.repo.create(movement)
        self.db.commit()
        return created

    def running_balance(self, as_of: Optional[datetime] = None) -> Decimal:
        return self._money(self.repo.get_running_balance(as_of=as_of))

    def owner_balance(self) -> Decimal:
        """Lo que el negocio le debe al socio: aportes menos retiros."""
        return self._money(self.repo.get_owner_balance())

    def ledger(
        self, start: Optional[datetime] = None, end: Optional[datetime] = None
    ) -> list[tuple[CashMovement, Decimal]]:
        movements = self.repo.get_all(start=start, end=end, limit=10_000, offset=0)
        balance = Decimal("0.00")
        out: list[tuple[CashMovement, Decimal]] = []
        for m in movements:
            balance += -m.amount if m.type in CASH_OUTFLOW_TYPES else m.amount
            out.append((m, self._money(balance)))
        return out
