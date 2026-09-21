"""Transpile a persisted model to any of the supported targets."""

from fastapi import APIRouter, Depends

from lexis.dispatch import transpile as dispatch_transpile
from lexis.parser import parse_ossie_yaml
from lexis.resolved_model import ResolvedModel
from lexis_api.deps import get_visible_model
from lexis_api.models import SemanticModelRecord
from lexis_api.schemas import TranspileIn, TranspileOut

router = APIRouter(prefix="/api/models", tags=["transpile"])


@router.post("/{model_id}/transpile", response_model=TranspileOut)
def transpile_model(
    body: TranspileIn,
    record: SemanticModelRecord = Depends(get_visible_model),
) -> TranspileOut:
    document = parse_ossie_yaml(record.raw_yaml)
    model = ResolvedModel.build(document.semantic_model[0])
    # A missing metric on a SQL target, or an option the target doesn't accept,
    # raises ValueError - already mapped to a 422 by main.py's global handler.
    result = dispatch_transpile(
        document, model, body.target, body.metric, body.group_by, body.options
    )
    return TranspileOut(content=result.content, warnings=result.warnings)
