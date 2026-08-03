"""TikTok Shop integration package with one authoritative endpoint registry."""

from app.integrations.tiktok.endpoints import ENDPOINTS, Endpoint, endpoint

__all__ = ["ENDPOINTS", "Endpoint", "endpoint"]