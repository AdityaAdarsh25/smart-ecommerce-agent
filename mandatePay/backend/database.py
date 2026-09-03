from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base,sessionmaker

DATABASE_URL="sqlite:///./mandatepay.db"

engine=create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread":False},
)

SessionLocal=sessionmaker(autocommit=False,autoflush=False, bind=engine)
Base=declarative_base()
def get_db():
    db=SessionLocal()
    try:
        yield db
    finally:
        db.close()


def utcnow_naive() -> datetime:
    """Current UTC time as a naive datetime.

    The `timestamp` columns are `DateTime` (no timezone), so every value
    written or compared against them must be naive UTC. Mixing aware and
    naive datetimes here would silently break time-window queries such as
    duplicate detection.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
