from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True)
class ProviderPayment:
    provider_payment_id: str
    status: str
    details: dict


class PaymentProviderAdapter:
    name: str

    async def create(self, *, idempotency_key: str) -> ProviderPayment:
        raise NotImplementedError


class MockPaymentProvider(PaymentProviderAdapter):
    name = "mock"

    async def create(self, *, idempotency_key: str) -> ProviderPayment:
        return ProviderPayment(
            provider_payment_id=str(uuid4()),
            status="pending",
            details={"adapter": "mock"},
        )


class WebhookPaymentProvider(PaymentProviderAdapter):
    """Provider-neutral pending adapter used until a vendor account is connected."""

    def __init__(self, name: str):
        self.name = name

    async def create(self, *, idempotency_key: str) -> ProviderPayment:
        return ProviderPayment(
            provider_payment_id=str(uuid4()),
            status="pending",
            details={"adapter": "webhook", "provider_setup_required": True},
        )


def get_payment_provider(name: str) -> PaymentProviderAdapter:
    if name == "mock":
        return MockPaymentProvider()
    return WebhookPaymentProvider(name)
