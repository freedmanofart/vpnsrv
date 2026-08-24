from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class VPNNode(Base):
    __tablename__ = "vpn_nodes"

    id: Mapped[int] = mapped_column(
        primary_key=True
    )

    name: Mapped[str] = mapped_column(
        String(128),
        unique=True,
        nullable=False,
    )

    provider: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    region: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    hostname: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    ip_address: Mapped[str] = mapped_column(
        String(45),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="active",
        index=True,
    )

    capacity: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=100,
    )

    health_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="unknown", index=True
    )

    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    active_connections: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
