from fastapi import APIRouter, HTTPException

from app.api.schemas import ExtractRequest
from app.schemas.aftersales import AfterSalesExtract
from app.services.extract_service import extract_aftersales


router = APIRouter(prefix="/api/v1", tags=["extract"])


@router.post("/extract", response_model=AfterSalesExtract)
async def extract(payload: ExtractRequest) -> AfterSalesExtract:
    try:
        return await extract_aftersales(payload.text)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="Upstream extraction failed",
        ) from exc
