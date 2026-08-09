from fastapi import APIRouter, Depends, HTTPException, Request

from contracts.fetch import FetchRequest, FetchResponse
from src.api.auth import require_scope
from src.api.deps import get_source_registry
from src.sources.base import (
    SourceFetcher,
    SourceForbidden,
    SourceNotConfigured,
    SourceNotFound,
    SourceUnavailable,
    SourceUnsupported,
)

router = APIRouter()


@router.post("/fetch", response_model=FetchResponse)
def fetch(
    body: FetchRequest,
    request: Request,
    _key=Depends(require_scope("fetch")),
    registry: dict[str, SourceFetcher] = Depends(get_source_registry),
) -> FetchResponse:
    request.state.audit_extra = {"source_id": body.source_id}
    source = registry.get(body.source_id)
    if source is None:
        raise HTTPException(status_code=422, detail=f"unsupported source: {body.source_id}")
    try:
        result = source.fetch(body.url)
    except SourceNotConfigured:
        raise HTTPException(status_code=503, detail="source not configured")
    except SourceForbidden:
        raise HTTPException(status_code=403, detail="source denied access to this file")
    except SourceNotFound:
        raise HTTPException(status_code=404, detail="file not found for this url")
    except SourceUnsupported as e:
        raise HTTPException(status_code=422, detail=str(e))
    except SourceUnavailable:
        raise HTTPException(status_code=502, detail="source upstream error")
    request.state.audit_extra["warnings"] = len(result.warnings)
    return FetchResponse(title=result.title, content=result.content, warnings=list(result.warnings))
