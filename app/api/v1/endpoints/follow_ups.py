import uuid

from fastapi import APIRouter, Depends, status

from app.api.dependencies import (
    CurrentUser,
    FollowUpServiceDep,
    SaleServiceDep,
    get_current_user,
)
from app.schemas.follow_up import FollowUpOut, FollowUpPostpone
from app.schemas.sale import FollowUpMetrics, PaginatedFollowUpResponse

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
    items, total = service.get_follow_ups(
        filter_type=filter, limit=limit, offset=offset
    )
    return {"data": items, "meta": {"total": total}}


@router.get("/metrics", response_model=FollowUpMetrics)
def get_follow_up_metrics(service: SaleServiceDep):
    return service.get_follow_up_metrics()


@router.post("/{follow_up_id}/postpone", response_model=FollowUpOut)
def postpone_follow_up(
    follow_up_id: uuid.UUID,
    body: FollowUpPostpone,
    service: FollowUpServiceDep,
    current_user: CurrentUser,
):
    """Cierra el seguimiento y abre otro en `date` (debe ser posterior a hoy)."""
    return service.postpone(follow_up_id, body.date, current_user.id)


@router.post("/{follow_up_id}/dismiss", status_code=status.HTTP_204_NO_CONTENT)
def dismiss_follow_up(
    follow_up_id: uuid.UUID,
    service: FollowUpServiceDep,
    current_user: CurrentUser,
):
    """Cierra el seguimiento sin abrir otro hasta la proxima venta del producto."""
    service.dismiss(follow_up_id, current_user.id)
