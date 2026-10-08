"""Server-owned Stripe configuration and hosted-session gateway."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from server.billing.errors import (
    BillingConfigurationError,
    BillingNotConfiguredError,
    BillingProviderError,
    InvalidWebhookSignatureError,
)
from server.billing.plan_catalog import PlanCatalog, get_plan_catalog

_PRICE_ID_PATTERN = re.compile(r"^price_[A-Za-z0-9]+$")
_CUSTOMER_ID_PATTERN = re.compile(r"^cus_[A-Za-z0-9]+$")
_SUBSCRIPTION_ID_PATTERN = re.compile(r"^sub_[A-Za-z0-9]+$")
_API_VERSION_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}(?:\.[a-z][a-z0-9_]*)?$")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True)
class StripeBillingConfig:
    enabled: bool
    secret_key: str | None = field(default=None, repr=False)
    webhook_secret: str | None = field(default=None, repr=False)
    checkout_success_url: str | None = None
    checkout_cancel_url: str | None = None
    portal_return_url: str | None = None
    api_version: str | None = None
    price_ids: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def require_price_id(self, plan_code: str) -> str:
        price_id = self.price_ids.get(str(plan_code or "").strip().lower())
        if not price_id:
            raise BillingConfigurationError(
                f"No server-owned Stripe Price is configured for plan '{plan_code}'."
            )
        return price_id

    def require_plan_code_for_price(self, price_id: str) -> str:
        normalized = str(price_id or "").strip()
        for plan_code, configured_price_id in self.price_ids.items():
            if configured_price_id == normalized:
                return plan_code
        raise BillingConfigurationError("Stripe subscription uses an unknown Price ID.")


def _billing_enabled(environment: Mapping[str, str]) -> bool:
    raw = str(environment.get("BILLING_ENABLED", "false") or "").strip().lower()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    raise BillingConfigurationError(
        "BILLING_ENABLED must be one of true/false, 1/0, yes/no, or on/off."
    )


def stripe_billing_is_enabled(
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Return the validated public availability flag without loading Stripe secrets."""
    return _billing_enabled(environment if environment is not None else os.environ)


def _required_environment_value(environment: Mapping[str, str], name: str) -> str:
    value = str(environment.get(name, "") or "").strip()
    if not value:
        raise BillingConfigurationError(f"Stripe billing requires {name}.")
    return value


def _validated_redirect_url(environment: Mapping[str, str], name: str) -> str:
    value = _required_environment_value(environment, name)
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").lower()
    if not parsed.scheme or not hostname or parsed.username or parsed.password:
        raise BillingConfigurationError(f"{name} must be an absolute URL without credentials.")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and hostname in _LOOPBACK_HOSTS):
        raise BillingConfigurationError(
            f"{name} must use HTTPS, except for an HTTP loopback development URL."
        )
    return value


def load_stripe_billing_config(
    *,
    catalog: PlanCatalog | None = None,
    environment: Mapping[str, str] | None = None,
) -> StripeBillingConfig:
    """Validate server-owned billing configuration without importing Stripe."""
    resolved_environment = environment if environment is not None else os.environ
    if not _billing_enabled(resolved_environment):
        return StripeBillingConfig(enabled=False)

    plan_catalog = catalog or get_plan_catalog()
    secret_key = _required_environment_value(resolved_environment, "STRIPE_SECRET_KEY")
    if not secret_key.startswith(("sk_test_", "sk_live_")):
        raise BillingConfigurationError(
            "STRIPE_SECRET_KEY must be a Stripe test or live secret key."
        )
    webhook_secret = _required_environment_value(resolved_environment, "STRIPE_WEBHOOK_SECRET")
    if not webhook_secret.startswith("whsec_") or len(webhook_secret) <= len("whsec_"):
        raise BillingConfigurationError(
            "STRIPE_WEBHOOK_SECRET must be a Stripe webhook signing secret."
        )

    price_ids: dict[str, str] = {}
    for plan in plan_catalog.list_plans():
        if plan.code == "free":
            continue
        if not plan.stripe_price_env:
            raise BillingConfigurationError(
                f"Paid plan '{plan.code}' does not declare a Stripe Price environment key."
            )
        price_id = _required_environment_value(resolved_environment, plan.stripe_price_env)
        if not _PRICE_ID_PATTERN.fullmatch(price_id):
            raise BillingConfigurationError(
                f"{plan.stripe_price_env} must contain a Stripe Price ID."
            )
        if price_id in price_ids.values():
            raise BillingConfigurationError(
                "Each paid subscription plan must use a distinct Stripe Price ID."
            )
        price_ids[plan.code] = price_id

    api_version = str(resolved_environment.get("STRIPE_API_VERSION", "") or "").strip() or None
    if api_version and not _API_VERSION_PATTERN.fullmatch(api_version):
        raise BillingConfigurationError(
            "STRIPE_API_VERSION must use Stripe's YYYY-MM-DD or YYYY-MM-DD.channel format."
        )

    return StripeBillingConfig(
        enabled=True,
        secret_key=secret_key,
        webhook_secret=webhook_secret,
        checkout_success_url=_validated_redirect_url(
            resolved_environment, "STRIPE_CHECKOUT_SUCCESS_URL"
        ),
        checkout_cancel_url=_validated_redirect_url(
            resolved_environment, "STRIPE_CHECKOUT_CANCEL_URL"
        ),
        portal_return_url=_validated_redirect_url(resolved_environment, "STRIPE_PORTAL_RETURN_URL"),
        api_version=api_version,
        price_ids=MappingProxyType(price_ids),
    )


def require_stripe_billing_config(*, catalog: PlanCatalog | None = None) -> StripeBillingConfig:
    config = load_stripe_billing_config(catalog=catalog)
    if not config.enabled:
        raise BillingNotConfiguredError("Stripe billing is disabled")
    return config


def _response_value(response: Any, name: str) -> Any:
    if isinstance(response, Mapping):
        return response.get(name)
    return getattr(response, name, None)


def _plain_mapping(response: Any) -> Mapping[str, Any] | None:
    if isinstance(response, Mapping):
        return response
    for method_name in ("to_dict_recursive", "to_dict"):
        method = getattr(response, method_name, None)
        if callable(method):
            converted = method()
            if isinstance(converted, Mapping):
                return converted
    return None


def provider_failure_details(error: BaseException) -> dict[str, Any]:
    """Return log-safe diagnostics for the Stripe SDK error behind a provider failure."""
    cause = error.__cause__ or error
    details: dict[str, Any] = {"provider_error_type": type(cause).__name__}
    for attribute in ("http_status", "code", "request_id"):
        value = getattr(cause, attribute, None)
        if value is not None:
            details[f"provider_{attribute}"] = value
    provider_error = getattr(cause, "error", None)
    message = getattr(provider_error, "message", None) or getattr(cause, "user_message", None)
    if not message and cause is not error:
        message = str(cause)
    if message:
        details["provider_message"] = str(message)[:500]
    return details


def _params_fingerprint(params: Mapping[str, Any]) -> str:
    encoded = json.dumps(params, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def _validated_hosted_url(response: Any, *, expected_host: str) -> str:
    value = str(_response_value(response, "url") or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != expected_host:
        raise BillingProviderError("Stripe returned an invalid hosted-session URL")
    return value


class StripeGateway:
    """Small async adapter around StripeClient; injectable for network-free tests."""

    def __init__(self, config: StripeBillingConfig, *, client: Any | None = None) -> None:
        if not config.enabled or not config.secret_key:
            raise BillingNotConfiguredError("Stripe billing is disabled")
        self.config = config
        self._client = client

    def _stripe_client(self) -> Any:
        if self._client is None:
            try:
                from stripe import StripeClient
            except ImportError as exc:
                raise BillingConfigurationError(
                    "The Stripe SDK is required when BILLING_ENABLED=true."
                ) from exc
            self._client = StripeClient(self.config.secret_key, max_network_retries=2)
        return self._client

    def _options(self, *, idempotency_key: str | None = None) -> dict[str, str] | None:
        options: dict[str, str] = {}
        if idempotency_key:
            options["idempotency_key"] = idempotency_key
        if self.config.api_version:
            options["stripe_version"] = self.config.api_version
        return options or None

    def verify_webhook_event(
        self,
        *,
        payload: bytes,
        signature: str,
    ) -> Mapping[str, Any]:
        """Verify the exact raw request body with Stripe's official SDK."""
        if not self.config.webhook_secret:
            raise BillingConfigurationError("Stripe webhook signing secret is missing")
        if not payload or not str(signature or "").strip():
            raise InvalidWebhookSignatureError("Stripe webhook signature is missing")
        try:
            import stripe
        except ImportError as exc:
            raise BillingConfigurationError(
                "The Stripe SDK is required when BILLING_ENABLED=true."
            ) from exc
        try:
            event = stripe.Webhook.construct_event(
                payload,
                signature,
                self.config.webhook_secret,
            )
        except (ValueError, stripe.error.SignatureVerificationError) as exc:
            raise InvalidWebhookSignatureError("Stripe webhook verification failed") from exc
        except Exception as exc:
            raise BillingConfigurationError("Stripe webhook verification is unavailable") from exc

        event_mapping = _plain_mapping(event)
        if event_mapping is None:
            raise InvalidWebhookSignatureError("Stripe webhook payload is invalid")
        return event_mapping

    async def retrieve_subscription(self, subscription_id: str) -> Mapping[str, Any]:
        normalized = str(subscription_id or "").strip()
        if not _SUBSCRIPTION_ID_PATTERN.fullmatch(normalized):
            raise BillingConfigurationError("Stripe Subscription ID is invalid")
        try:
            response = await self._stripe_client().v1.subscriptions.retrieve_async(
                normalized,
                options=self._options(),
            )
        except (BillingConfigurationError, BillingProviderError):
            raise
        except Exception as exc:
            raise BillingProviderError("Stripe Subscription retrieval failed") from exc
        response_mapping = _plain_mapping(response)
        if response_mapping is None:
            raise BillingProviderError("Stripe returned an invalid Subscription")
        return response_mapping

    async def list_customer_subscriptions(
        self,
        *,
        customer_id: str,
    ) -> tuple[Mapping[str, Any], ...]:
        if not _CUSTOMER_ID_PATTERN.fullmatch(str(customer_id or "").strip()):
            raise BillingConfigurationError("Persisted Stripe Customer ID is invalid")
        try:
            response = await self._stripe_client().v1.subscriptions.list_async(
                {"customer": customer_id, "status": "all", "limit": 100},
                options=self._options(),
            )
        except (BillingConfigurationError, BillingProviderError):
            raise
        except Exception as exc:
            raise BillingProviderError("Stripe Subscription listing failed") from exc
        response_mapping = _plain_mapping(response)
        if response_mapping is None:
            raise BillingProviderError("Stripe returned an invalid Subscription list")
        if bool(_response_value(response_mapping, "has_more")):
            raise BillingProviderError(
                "Stripe returned too many Subscriptions for automatic reconciliation"
            )
        data = _response_value(response_mapping, "data")
        if not isinstance(data, list) or not all(isinstance(item, Mapping) for item in data):
            raise BillingProviderError("Stripe returned an invalid Subscription list")
        return tuple(data)

    async def create_customer(
        self,
        *,
        billing_account_id: UUID,
        user_id: UUID,
        email: str | None,
    ) -> str:
        params: dict[str, Any] = {
            "metadata": {
                "cortex_billing_account_id": str(billing_account_id),
                "cortex_user_id": str(user_id),
            }
        }
        if email:
            params["email"] = email
        try:
            response = await self._stripe_client().v1.customers.create_async(
                params,
                options=self._options(idempotency_key=f"cortex-customer-{billing_account_id}"),
            )
        except (BillingConfigurationError, BillingProviderError):
            raise
        except Exception as exc:
            raise BillingProviderError("Stripe Customer creation failed") from exc

        customer_id = str(_response_value(response, "id") or "").strip()
        if not _CUSTOMER_ID_PATTERN.fullmatch(customer_id):
            raise BillingProviderError("Stripe returned an invalid Customer ID")
        return customer_id

    async def create_checkout_session(
        self,
        *,
        billing_account_id: UUID,
        user_id: UUID,
        customer_id: str,
        plan_code: str,
        price_id: str,
        idempotency_key: str,
    ) -> str:
        if not _CUSTOMER_ID_PATTERN.fullmatch(customer_id):
            raise BillingConfigurationError("Persisted Stripe Customer ID is invalid")
        if not _PRICE_ID_PATTERN.fullmatch(price_id):
            raise BillingConfigurationError("Configured Stripe Price ID is invalid")
        if not self.config.checkout_success_url or not self.config.checkout_cancel_url:
            raise BillingConfigurationError("Stripe Checkout redirect URLs are missing")

        metadata = {
            "cortex_billing_account_id": str(billing_account_id),
            "cortex_user_id": str(user_id),
            "cortex_plan_code": plan_code,
        }
        params = {
            "mode": "subscription",
            "customer": customer_id,
            "line_items": [{"price": price_id, "quantity": 1}],
            "success_url": self.config.checkout_success_url,
            "cancel_url": self.config.checkout_cancel_url,
            "client_reference_id": str(billing_account_id),
            "metadata": metadata,
            "ui_mode": "hosted_page",
            "origin_context": "web",
            "submit_type": "auto",
            "billing_address_collection": "auto",
            "phone_number_collection": {"enabled": False},
            "automatic_tax": {"enabled": False},
            # Dashboard-managed promotion codes; a code that makes the total $0
            # completes Checkout without collecting a payment method.
            "allow_promotion_codes": True,
            "payment_method_collection": "if_required",
            "subscription_data": {
                "metadata": {
                    "cortex_billing_account_id": str(billing_account_id),
                    "cortex_plan_code": plan_code,
                }
            },
        }
        # Stripe rejects a reused idempotency key whose parameters differ, and replays a
        # cached 4xx for 24h; scoping the key to the exact parameters lets a corrected
        # Customer, Price, or redirect URL take effect immediately.
        scoped_idempotency_key = f"{idempotency_key}-{_params_fingerprint(params)}"
        try:
            response = await self._stripe_client().v1.checkout.sessions.create_async(
                params,
                options=self._options(idempotency_key=scoped_idempotency_key),
            )
        except (BillingConfigurationError, BillingProviderError):
            raise
        except Exception as exc:
            raise BillingProviderError("Stripe Checkout Session creation failed") from exc
        return _validated_hosted_url(response, expected_host="checkout.stripe.com")

    async def create_portal_session(self, *, customer_id: str) -> str:
        if not _CUSTOMER_ID_PATTERN.fullmatch(customer_id):
            raise BillingConfigurationError("Persisted Stripe Customer ID is invalid")
        if not self.config.portal_return_url:
            raise BillingConfigurationError("Stripe Portal return URL is missing")
        try:
            response = await self._stripe_client().v1.billing_portal.sessions.create_async(
                {
                    "customer": customer_id,
                    "return_url": self.config.portal_return_url,
                },
                options=self._options(),
            )
        except (BillingConfigurationError, BillingProviderError):
            raise
        except Exception as exc:
            raise BillingProviderError("Stripe Portal Session creation failed") from exc
        return _validated_hosted_url(response, expected_host="billing.stripe.com")
