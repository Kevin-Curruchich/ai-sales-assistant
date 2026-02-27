from pydantic import BaseModel
from app.schemas.sale import SaleResponse, FollowUpResponse


class DashboardSummary(BaseModel):
    totalCustomers: int
    salesThisMonth: float
    pendingFollowUps: int
    upcomingPurchases7Days: int
    recentSales: list[SaleResponse] = []
    priorityCustomers: list[FollowUpResponse] = []
