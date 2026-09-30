import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentThread


class AgentThreadRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, owner_id: uuid.UUID, title: str) -> AgentThread:
        thread = AgentThread(owner_id=owner_id, title=title)
        self.db.add(thread)
        self.db.flush()
        self.db.refresh(thread)
        return thread

    def get(self, thread_id: uuid.UUID) -> Optional[AgentThread]:
        return self.db.get(AgentThread, thread_id)

    def list_for_owner(self, owner_id: uuid.UUID) -> list[AgentThread]:
        stmt = (
            select(AgentThread)
            .where(AgentThread.owner_id == owner_id)
            .order_by(AgentThread.created_at, AgentThread.id)
        )
        return list(self.db.execute(stmt).scalars().all())

    def rename(self, thread_id: uuid.UUID, title: str) -> None:
        thread = self.get(thread_id)
        if thread is not None:
            thread.title = title
            self.db.flush()

    def delete(self, thread_id: uuid.UUID) -> None:
        thread = self.get(thread_id)
        if thread is not None:
            self.db.delete(thread)
            self.db.flush()
