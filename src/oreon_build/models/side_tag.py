from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import DateTime, Enum, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from oreon_build.db.base import Base

if TYPE_CHECKING:
    from .account import Account
    from .build import BuildJob
    from .build_chain import BuildChain
    from .release import Release


class SideTagCreatedFrom(str, enum.Enum):
    MANUAL = "manual"
    GITHUB_PR = "github_pr"
    GITHUB_PUSH = "github_push"


class SideTag(Base):
    __tablename__ = "side_tags"
    __table_args__ = (UniqueConstraint("release_id", "name", name="uq_side_tags_release_name"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    release_id: Mapped[int] = mapped_column(ForeignKey("releases.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_account_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("accounts.id"), nullable=True, index=True
    )
    created_from: Mapped[SideTagCreatedFrom] = mapped_column(
        Enum(
            SideTagCreatedFrom,
            values_callable=lambda x: [e.value for e in x],
            native_enum=False,
            length=32,
        ),
        default=SideTagCreatedFrom.MANUAL,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    release: Mapped["Release"] = relationship("Release")
    owner: Mapped[Optional["Account"]] = relationship("Account", foreign_keys=[owner_account_id])
    members: Mapped[List["SideTagMember"]] = relationship(
        "SideTagMember", back_populates="side_tag", cascade="all, delete-orphan"
    )
    jobs: Mapped[List["BuildJob"]] = relationship("BuildJob", back_populates="side_tag")
    chains: Mapped[List["BuildChain"]] = relationship("BuildChain", back_populates="side_tag")


class SideTagMember(Base):
    __tablename__ = "side_tag_members"
    __table_args__ = (
        UniqueConstraint("side_tag_id", "account_id", name="uq_side_tag_members_tag_account"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    side_tag_id: Mapped[int] = mapped_column(ForeignKey("side_tags.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    side_tag: Mapped["SideTag"] = relationship("SideTag", back_populates="members")
    account: Mapped["Account"] = relationship("Account")
