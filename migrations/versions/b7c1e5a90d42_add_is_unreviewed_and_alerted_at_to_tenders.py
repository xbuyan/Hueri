"""add is_unreviewed and alerted_at to tenders

Revision ID: b7c1e5a90d42
Revises: da60cbaf67b1
Create Date: 2026-10-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b7c1e5a90d42"
down_revision: Union[str, Sequence[str], None] = "da60cbaf67b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "tenders",
        sa.Column("is_unreviewed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("tenders", sa.Column("alerted_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("tenders") as batch_op:
        batch_op.drop_column("alerted_at")
        batch_op.drop_column("is_unreviewed")
