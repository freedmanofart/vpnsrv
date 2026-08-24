from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class VPNNodeConfig(Base):
    __tablename__ = "vpn_node_configs"

    id: Mapped[int] = mapped_column(
        primary_key=True
    )

    node_id: Mapped[int] = mapped_column(
        ForeignKey("vpn_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    protocol: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    config: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
    )

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
