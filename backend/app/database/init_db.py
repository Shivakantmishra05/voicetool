from app.database.base import Base
from app.database.session import engine
from app.models import call  # noqa: F401


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

