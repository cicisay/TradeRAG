"""llm provider config (multi-model management)

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-09 19:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('llm_provider_config',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('base_url', sa.String(length=255), nullable=False),
    sa.Column('model', sa.String(length=64), nullable=False),
    sa.Column('api_key_enc', sa.String(length=512), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('is_primary', sa.Boolean(), nullable=False),
    sa.Column('remark', sa.String(length=255), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_llm_provider_config_id'), 'llm_provider_config', ['id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_llm_provider_config_id'), table_name='llm_provider_config')
    op.drop_table('llm_provider_config')
