"""Low-coupling, read-only CPA quota API and card rendering primitives."""

from .api import CPAUsageClient, CPAUsageError, QuotaRequestConfig
from .assets import AssetError, AssetResolver
from .quota_models import QuotaAccount, QuotaSnapshot, QuotaWindow, ResetCredit
from .quota_render import QuotaCardRenderer
from .settings import as_bool, client_from_config, find_provider_config

__all__ = [
    "CPAUsageClient",
    "CPAUsageError",
    "AssetError",
    "AssetResolver",
    "QuotaRequestConfig",
    "QuotaAccount",
    "QuotaSnapshot",
    "QuotaWindow",
    "ResetCredit",
    "QuotaCardRenderer",
    "client_from_config",
    "find_provider_config",
    "as_bool",
]
