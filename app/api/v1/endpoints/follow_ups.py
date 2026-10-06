from fastapi import APIRouter, Depends
from app.api.dependencies import SaleServiceDep, get_current_user
from app.schemas.sale import PaginatedFollowUpResponse, FollowUpMetrics

router = APIRouter(
    prefix="/follow-ups",
    tags=["Follow-ups"],
    dependencies=[Depends(get_current_user)],
)


@router.get("", response_model=PaginatedFollowUpResponse)
def list_follow_ups(
    service: SaleServiceDep,
    filter: str = "all",
    limit: int = 10,
    offset: int = 0,
):
    """Get follow-up list with pagination.

    filter options: all, overdue, 7_days, 14_days, 30_days
    """
    items, total = service.get_follow_ups(filter_type=filter, limit=limit, offset=offset)
    return {"data": items, "meta": {"total": total}}


@router.get("/metrics", response_model=FollowUpMetrics)
def get_follow_up_metrics(service: SaleServiceDep):
    return service.get_follow_up_metrics()
