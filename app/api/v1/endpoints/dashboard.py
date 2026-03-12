from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.api.dependencies import get_db, get_current_user
from app.schemas.dashboard import DashboardSummary
from app.services.customer_service import CustomerService
from app.services.sale_service import SaleService

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


@router.get("/summary", response_model=DashboardSummary)
def get_dashboard_summary(
    db: Session = Depends(get_db),
    _current_user: dict = Depends(get_current_user),
):
    customer_service = CustomerService(db)
    sale_service = SaleService(db)

    total_customers = customer_service.count()
    sales_this_month = sale_service.get_sales_this_month()
    follow_up_metrics = sale_service.get_follow_up_metrics()
    recent_sales = sale_service.get_all_enriched(limit=5, offset=0)
    priority_customers, _ = sale_service.get_follow_ups(filter_type="7_days", limit=100, offset=0)

    return DashboardSummary(
        totalCustomers=total_customers,
        salesThisMonth=sales_this_month,
        pendingFollowUps=follow_up_metrics.overdue,
        upcomingPurchases7Days=follow_up_metrics.next7Days,
        recentSales=recent_sales,
        priorityCustomers=priority_customers,
    )
