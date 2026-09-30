"""El hilo es un objeto con dueno.  Su id es el mismo que usa el checkpointer:
una sola identidad por conversacion, no dos que haya que mantener en sincronia."""

import uuid

from app.models import AgentThread
from app.repositories.agent_thread_repository import AgentThreadRepository


def test_a_thread_belongs_to_the_user_that_created_it(db_session, seeded_user):
    repo = AgentThreadRepository(db_session)
    thread = repo.create(owner_id=seeded_user.id, title="vendi dos cartones")

    assert thread.owner_id == seeded_user.id
    assert isinstance(thread.id, uuid.UUID)
    assert thread.title == "vendi dos cartones"


def test_listing_returns_only_the_owners_threads(db_session, seeded_user, second_user):
    repo = AgentThreadRepository(db_session)
    mio = repo.create(owner_id=seeded_user.id, title="mio")
    repo.create(owner_id=second_user.id, title="del otro")

    listados = repo.list_for_owner(seeded_user.id)

    assert [t.id for t in listados] == [mio.id]


def test_renaming_and_deleting(db_session, seeded_user):
    repo = AgentThreadRepository(db_session)
    thread = repo.create(owner_id=seeded_user.id, title="sin titulo")

    repo.rename(thread.id, "ventas de la semana")
    assert repo.get(thread.id).title == "ventas de la semana"

    repo.delete(thread.id)
    assert repo.get(thread.id) is None
