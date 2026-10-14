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
    if not tables or tables == {'alembic_version'}:
        _create_fresh_schema()
        return
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


def _create_fresh_schema():
    """Create the complete schema when upgrading a brand-new database."""
    op.create_table('business_categories',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('slug', sa.String(length=80), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'), sa.UniqueConstraint('name'), sa.UniqueConstraint('slug'))
    op.create_index('ix_business_categories_slug', 'business_categories', ['slug'], unique=True)
    op.create_table('businesses',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('slug', sa.String(length=120), nullable=False),
        sa.Column('category_id', sa.Integer(), nullable=True),
        sa.Column('logo_url', sa.String(length=255), nullable=True),
        sa.Column('logo_path', sa.String(length=255), nullable=True),
        sa.Column('cover_path', sa.String(length=255), nullable=True),
        sa.Column('primary_color', sa.String(length=7), nullable=False),
        sa.Column('secondary_color', sa.String(length=7), nullable=False),
        sa.Column('loyalty_type', sa.String(length=20), nullable=False),
        sa.Column('points_per_currency', sa.Float(), nullable=False),
        sa.Column('stamps_reward_limit', sa.Integer(), nullable=False),
        sa.Column('latitude', sa.Float(), nullable=True), sa.Column('longitude', sa.Float(), nullable=True),
        sa.Column('cooldown_minutes', sa.Integer(), nullable=False),
        sa.Column('reward_description', sa.String(length=255), nullable=True),
        sa.Column('wallet_terms', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False), sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['category_id'], ['business_categories.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'), sa.UniqueConstraint('slug'))
    op.create_index('ix_businesses_category_id', 'businesses', ['category_id'], unique=False)
    op.create_index('ix_businesses_slug', 'businesses', ['slug'], unique=True)
    op.create_table('platform_admins',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False), sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('email', sa.String(length=120), nullable=False), sa.Column('phone', sa.String(length=30), nullable=True),
        sa.Column('password_hash', sa.String(length=255), nullable=False), sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False), sa.PrimaryKeyConstraint('id'), sa.UniqueConstraint('email'))
    op.create_index('ix_platform_admins_email', 'platform_admins', ['email'], unique=True)
    op.create_table('customers',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False), sa.Column('business_id', sa.Integer(), nullable=False),
        sa.Column('full_name', sa.String(length=120), nullable=False), sa.Column('email', sa.String(length=120), nullable=True),
        sa.Column('phone', sa.String(length=30), nullable=True), sa.Column('birthdate', sa.Date(), nullable=True),
        sa.Column('current_points', sa.Float(), nullable=False), sa.Column('current_stamps', sa.Integer(), nullable=False),
        sa.Column('qr_code_token', sa.String(length=64), nullable=False), sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], ondelete='CASCADE'), sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('qr_code_token'))
    for column in ('business_id', 'email', 'phone'):
        op.create_index(f'ix_customers_{column}', 'customers', [column], unique=False)
    op.create_index('ix_customers_qr_code_token', 'customers', ['qr_code_token'], unique=True)
    op.create_table('staff',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False), sa.Column('business_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=True), sa.Column('username', sa.String(length=80), nullable=False),
        sa.Column('email', sa.String(length=120), nullable=True), sa.Column('phone', sa.String(length=30), nullable=True),
        sa.Column('password_hash', sa.String(length=255), nullable=False), sa.Column('role', sa.String(length=20), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False), sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], ondelete='CASCADE'), sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('business_id', 'username', name='uq_business_username'))
    op.create_index('ix_staff_business_id', 'staff', ['business_id'], unique=False)
    op.create_table('transactions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False), sa.Column('business_id', sa.Integer(), nullable=False),
        sa.Column('customer_id', sa.Integer(), nullable=False), sa.Column('staff_id', sa.Integer(), nullable=True),
        sa.Column('type', sa.String(length=20), nullable=False), sa.Column('points_amount', sa.Float(), nullable=False),
        sa.Column('stamps_amount', sa.Integer(), nullable=False), sa.Column('purchase_amount', sa.Float(), nullable=False),
        sa.Column('timestamp', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['staff_id'], ['staff.id'], ondelete='SET NULL'), sa.PrimaryKeyConstraint('id'))
    for column in ('business_id', 'customer_id', 'staff_id', 'timestamp'):
        op.create_index(f'ix_transactions_{column}', 'transactions', [column], unique=False)
    op.create_table('apple_devices',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False), sa.Column('customer_id', sa.Integer(), nullable=False),
        sa.Column('device_library_identifier', sa.String(length=120), nullable=False), sa.Column('push_token', sa.String(length=255), nullable=False),
        sa.Column('pass_type_identifier', sa.String(length=120), nullable=False), sa.Column('serial_number', sa.String(length=120), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False), sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'), sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('device_library_identifier', 'pass_type_identifier', 'serial_number', name='uq_device_pass_registration'))
    for column in ('customer_id', 'device_library_identifier', 'serial_number'):
        op.create_index(f'ix_apple_devices_{column}', 'apple_devices', [column], unique=False)



def downgrade():
    with op.batch_alter_table('businesses', schema=None) as batch_op:
        batch_op.drop_column('cover_path')
        batch_op.drop_column('logo_path')

    op.drop_index(op.f('ix_platform_admins_email'), table_name='platform_admins')
    op.drop_table('platform_admins')
