"""add platform admins and business branding paths

Revision ID: 9a85c3791398
Revises:
Create Date: 2026-10-14 14:41:21.933463

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9a85c3791398'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if 'platform_admins' not in tables:
        op.create_table(
            'platform_admins',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('name', sa.String(length=120), nullable=False),
            sa.Column('email', sa.String(length=120), nullable=False),
            sa.Column('phone', sa.String(length=30), nullable=True),
            sa.Column('password_hash', sa.String(length=255), nullable=False),
            sa.Column('is_active', sa.Boolean(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('email'),
        )
        op.create_index(op.f('ix_platform_admins_email'), 'platform_admins', ['email'], unique=True)
    business_columns = {column['name'] for column in inspector.get_columns('businesses')}
    with op.batch_alter_table('businesses', schema=None) as batch_op:
        if 'logo_path' not in business_columns:
            batch_op.add_column(sa.Column('logo_path', sa.String(length=255), nullable=True))
        if 'cover_path' not in business_columns:
            batch_op.add_column(sa.Column('cover_path', sa.String(length=255), nullable=True))



def downgrade():
    with op.batch_alter_table('businesses', schema=None) as batch_op:
        batch_op.drop_column('cover_path')
        batch_op.drop_column('logo_path')

    op.drop_index(op.f('ix_platform_admins_email'), table_name='platform_admins')
    op.drop_table('platform_admins')
