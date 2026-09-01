"""update manual bank payment methods

Revision ID: f51ac870d296
Revises: e4a9b03c127d
"""

from alembic import op


revision = "f51ac870d296"
down_revision = "e4a9b03c127d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE payment_methods SET code='sber_qr', name='Сбербанк, QR (рубли)', url=NULL WHERE code='sbp'")
    op.execute("UPDATE payment_methods SET code='tbank_qr', name='Т-Банк, QR (рубли)', url=NULL WHERE code='card_ru'")
    op.execute("UPDATE payment_methods SET code='phone_transfer', name='Перевод по номеру телефона', url=NULL WHERE code='card_foreign'")


def downgrade() -> None:
    op.execute("UPDATE payment_methods SET code='sbp', name='СБП, QR (рубли)' WHERE code='sber_qr'")
    op.execute("UPDATE payment_methods SET code='card_ru', name='Карта (рубли)' WHERE code='tbank_qr'")
    op.execute("UPDATE payment_methods SET code='card_foreign', name='Зарубежная карта' WHERE code='phone_transfer'")
