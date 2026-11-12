"""AI extra services: viral marketing kit & thumbnail generation."""

from backend.services.ai_extras.marketing_kit import MarketingKit, MarketingKitService
from backend.services.ai_extras.thumbnail_gen import ThumbnailResult, ThumbnailService

__all__ = [
    "MarketingKit",
    "MarketingKitService",
    "ThumbnailResult",
    "ThumbnailService",
]
