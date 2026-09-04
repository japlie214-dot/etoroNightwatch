"""eToro API client (DESIGN §1.1, §2, §4.3, §5, §6). Built in step 5, against demo.

Submodules: :mod:`auth`, :mod:`models`, :mod:`errors`, :mod:`ratelimit`,
:mod:`client`.
"""

from .client import EtoroClient, MAX_LEVERAGE, SETTLEMENT_TYPE, TRANSACTION
from .errors import (
    EtoroAuthError,
    EtoroEligibilityError,
    EtoroEnvironmentMismatchError,
    EtoroError,
    EtoroNetworkError,
    EtoroOrderRejected,
    EtoroRateLimitError,
    EtoroServerError,
    EtoroTimeoutError,
    EtoroValidationError,
)
from .models import OrderStatus

__all__ = [
    "EtoroClient",
    "MAX_LEVERAGE",
    "TRANSACTION",
    "SETTLEMENT_TYPE",
    "EtoroError",
    "EtoroAuthError",
    "EtoroEnvironmentMismatchError",
    "EtoroRateLimitError",
    "EtoroValidationError",
    "EtoroServerError",
    "EtoroNetworkError",
    "EtoroTimeoutError",
    "EtoroOrderRejected",
    "EtoroEligibilityError",
    "OrderStatus",
]
