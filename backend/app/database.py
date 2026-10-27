"""SQLAlchemy database setup for persisted simulated orders."""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./trademind.db")
is_sqlite = DATABASE_URL.startswith("sqlite")
connect_args = {"check_same_thread": False} if is_sqlite else {}
pool_options = {"poolclass": StaticPool} if DATABASE_URL in {"sqlite://", "sqlite:///:memory:"} else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True, **pool_options)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

class Base(DeclarativeBase):
    pass
