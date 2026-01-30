"""Core types and enums for GRINDER."""

from enum import Enum


class GridMode(Enum):
    """Grid operation modes."""

    BILATERAL = "BILATERAL"  # Range-bound: both sides
    UNI_LONG = "UNI_LONG"  # Trend up: buy-side only
    UNI_SHORT = "UNI_SHORT"  # Trend down: sell-side only
    THROTTLE = "THROTTLE"  # Reduced activity
    PAUSE = "PAUSE"  # No new orders
    EMERGENCY = "EMERGENCY"  # Exit only


class SystemState(Enum):
    """System state machine states."""

    INIT = "INIT"  # Starting up
    READY = "READY"  # Connected, waiting for Top-K
    ACTIVE = "ACTIVE"  # Normal operation
    THROTTLED = "THROTTLED"  # Reduced activity
    PAUSED = "PAUSED"  # No new orders
    DEGRADED = "DEGRADED"  # Data issues
    EMERGENCY = "EMERGENCY"  # Risk breach


class ToxicityLevel(Enum):
    """Toxicity classification levels."""

    LOW = "LOW"  # tox < 1.0
    MID = "MID"  # 1.0 <= tox < 2.0
    HIGH = "HIGH"  # tox >= 2.0


class OrderSide(Enum):
    """Order side."""

    BUY = "BUY"
    SELL = "SELL"


class OrderType(Enum):
    """Order type."""

    LIMIT = "LIMIT"
    MARKET = "MARKET"
    LIMIT_MAKER = "LIMIT_MAKER"


class OrderState(Enum):
    """Order lifecycle state."""

    PENDING = "PENDING"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
