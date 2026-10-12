import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models.follow_up import OPEN, SYNC_FAILED, SYNC_PENDING, FollowUp


class FollowUpRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_id(self, follow_up_id: uuid.UUID) -> Optional[FollowUp]:
        return self.db.get(FollowUp, follow_up_id)

    def get_by_id_for_update(self, follow_up_id: uuid.UUID) -> Optional[FollowUp]:
        stmt = select(FollowUp).where(FollowUp.id == follow_up_id).with_for_update()
        return self.db.execute(stmt).scalar_one_or_none()

    def get_open(
        self, customer_id: uuid.UUID, product_id: uuid.UUID
    ) -> Optional[FollowUp]:
        stmt = select(FollowUp).where(
            FollowUp.customer_id == customer_id,
            FollowUp.product_id == product_id,
            FollowUp.status == OPEN,
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def get_open_with_relations(self) -> list[FollowUp]:
        stmt = (
            select(FollowUp)
            .options(joinedload(FollowUp.customer), joinedload(FollowUp.product))
            .where(FollowUp.status == OPEN)
            .order_by(FollowUp.due_date.asc())
        )
        return list(self.db.execute(stmt).unique().scalars().all())

    def get_to_sync(self) -> list[FollowUp]:
        stmt = (
            select(FollowUp)
            .options(joinedload(FollowUp.customer), joinedload(FollowUp.product))
            .where(FollowUp.sync_status.in_([SYNC_PENDING, SYNC_FAILED]))
            .order_by(FollowUp.updated_at.asc())
        )
        return list(self.db.execute(stmt).unique().scalars().all())

    def add(self, follow_up: FollowUp) -> None:
        self.db.add(follow_up)
