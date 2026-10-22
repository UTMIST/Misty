from fastapi import APIRouter, Depends, HTTPException, Request

from contracts.discovery import DiscoveryPage, DiscoveryRequest
from src.api.auth import require_scope
from src.api.deps import get_discovery_source
from src.sources.base import (
    DiscoverySource,
    SourceForbidden,
    SourceNotConfigured,
    SourceNotFound,
    SourceUnavailable,
)

router = APIRouter()


@router.post("/discover/google", response_model=DiscoveryPage)
def discover(
    body: DiscoveryRequest,
    request: Request,
    _key=Depends(require_scope("discover")),
    source: DiscoverySource = Depends(get_discovery_source),
) -> DiscoveryPage:
    try:
        page = source.discover(body)
    except SourceNotConfigured:
        raise HTTPException(503, "source not configured") from None
    except SourceForbidden:
        raise HTTPException(403, "source denied listing") from None
    except SourceNotFound:
        raise HTTPException(404, "source folder not found") from None
    except SourceUnavailable:
        raise HTTPException(502, "source listing unavailable") from None
    request.state.audit_extra = {"items": len(page.items), "incomplete": page.incomplete}
    return page
