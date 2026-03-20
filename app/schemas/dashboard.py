from pydantic import BaseModel
from decimal import Decimal
from app.schemas.sale import SaleResponse, FollowUpResponse


class DashboardSummary(BaseModel):
    totalCustomers: int
    salesThisMonth: Decimal
    pendingFollowUps: int
    upcomingPurchases7Days: int
    recentSales: list[SaleResponse] = []
    priorityCustomers: list[FollowUpResponse] = []
