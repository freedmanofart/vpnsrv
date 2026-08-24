from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class VPNClient(Base):
    __tablename__ = "vpn_clients"
    __table_args__ = (
        Index(
            "uq_vpn_clients_one_active_per_subscription",
            "subscription_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )

    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id"),
        nullable=False,
        index=True,
    )

    node_id: Mapped[int] = mapped_column(
        ForeignKey("vpn_nodes.id"),
        nullable=False,
        index=True,
    )

    protocol: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    client_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="universal"
    )

    flow: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )

    fingerprint: Mapped[str] = mapped_column(
        String(32), nullable=False, default="chrome"
    )

    client_uuid: Mapped[str] = mapped_column(
        String(36),
        unique=True,
        nullable=False,
        index=True,
    )

    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="active",
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )

    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
