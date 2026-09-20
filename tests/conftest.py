from pathlib import Path

import pytest

from lexis.parser import load_ossie_document
from lexis.resolved_model import ResolvedModel

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def tpcds_document():
    return load_ossie_document(FIXTURES / "tpcds_semantic_model.yaml")


@pytest.fixture
def tpcds_model(tpcds_document):
    return ResolvedModel.build(tpcds_document.semantic_model[0])


@pytest.fixture
def sml_repo_dir():
    """A small hand-authored SML repo exercising Phase 2's parser: snowflake-free
    dimension attribute flattening, a 2-level hierarchy, a degenerate time
    dimension, a plain metric, a ratio metric_calc, an arbitrary-MDX metric_calc,
    and an unsupported (row_security) object type."""
    return FIXTURES / "sml"


@pytest.fixture
def retail_model():
    """The bundled multi-fact retail model: two fact tables over five conformed
    dimensions, which exercises emitter behaviour the single-fact TPC-DS fixture
    cannot (per-fact explores, chasm-trap avoidance)."""
    document = load_ossie_document(
        Path(__file__).resolve().parent.parent
        / "src"
        / "lexis_api"
        / "sample_data"
        / "retail_analytics_model.yaml"
    )
    return ResolvedModel.build(document.semantic_model[0])
