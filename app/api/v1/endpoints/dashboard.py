from fastapi import APIRouter, Depends

from app.api.dependencies import CustomerServiceDep, SaleServiceDep, get_current_user
from app.schemas.dashboard import DashboardSummary

router = APIRouter(
    prefix="/dashboard",
    tags=["Dashboard"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/summary", response_model=DashboardSummary)
def get_dashboard_summary(
    customer_service: CustomerServiceDep,
    sale_service: SaleServiceDep,
):
    total_customers = customer_service.count()
    sales_this_month = sale_service.get_sales_this_month()
    follow_up_metrics = sale_service.get_follow_up_metrics()
    recent_sales = sale_service.get_all_enriched(limit=5, offset=0)
    priority_customers, _ = sale_service.get_follow_ups(
        filter_type="7_days", limit=100, offset=0
    )

    return DashboardSummary(
        totalCustomers=total_customers,
        salesThisMonth=sales_this_month,
        pendingFollowUps=follow_up_metrics.overdue,
        upcomingPurchases7Days=follow_up_metrics.next7Days,
        pendingPaymentsTotal=sale_service.sum_total(is_payment_pending=True),
        pendingPaymentsCount=sale_service.count(is_payment_pending=True),
        recentSales=recent_sales,
        priorityCustomers=priority_customers,
    )
