from pydantic import BaseModel
from decimal import Decimal
from app.schemas.sale import SaleResponse, FollowUpResponse


class DashboardSummary(BaseModel):
    totalCustomers: int
    salesThisMonth: Decimal
    pendingFollowUps: int
    upcomingPurchases7Days: int
    # Cuentas por cobrar: ventas pendientes de pago, de cualquier fecha.
    pendingPaymentsTotal: Decimal = Decimal("0")
    pendingPaymentsCount: int = 0
    recentSales: list[SaleResponse] = []
    priorityCustomers: list[FollowUpResponse] = []
