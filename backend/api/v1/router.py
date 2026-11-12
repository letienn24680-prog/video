"""API v1 router aggregating health, provider discovery, and project endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from backend.api.v1 import (
    billing,
    health,
    media_review,
    projects,
    providers,
    studio_features,
    system_account,
)

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(health.router)
api_v1_router.include_router(providers.router)
api_v1_router.include_router(studio_features.router)
api_v1_router.include_router(projects.router)
api_v1_router.include_router(media_review.router)
api_v1_router.include_router(billing.router)
api_v1_router.include_router(system_account.router)

router = api_v1_router
