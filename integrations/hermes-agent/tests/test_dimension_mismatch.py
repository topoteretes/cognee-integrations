import pytest
import unittest.mock as mock
import sys
from pathlib import Path

# Add hermes-agent package to path if not present
pkg_dir = Path(__file__).parent.parent
if str(pkg_dir) not in sys.path:
    sys.path.insert(0, str(pkg_dir))

from cognee_integration_hermes.provider import _check_embedding_dimensions

@pytest.mark.asyncio
async def test_dimension_mismatch_raised_hermes():
    # Setup mock objects
    mock_vector_engine = mock.MagicMock()
    mock_embedding_engine = mock.MagicMock()

    # Setup mock embedding engine size and model:
    mock_embedding_engine.get_vector_size.return_value = 1024
    mock_embedding_engine.model = "openai/text-embedding-3-large"

    # Setup mock connection and table names
    mock_conn = mock.MagicMock()
    mock_conn.table_names = mock.AsyncMock(return_value=["TestCollection_text"])
    mock_vector_engine.get_connection = mock.AsyncMock(return_value=mock_conn)

    # Setup mock collection
    mock_collection = mock.MagicMock()
    mock_field = mock.MagicMock()
    # Set list_size to 384 (which mismatches query_dim 1024)
    mock_field.type = mock.MagicMock(list_size=384)
    mock_collection.schema.field.return_value = mock_field
    mock_vector_engine.get_collection = mock.AsyncMock(return_value=mock_collection)

    # Replace the functions returned by imports:
    with mock.patch("cognee.infrastructure.databases.vector.get_vector_engine.get_vector_engine", return_value=mock_vector_engine), \
         mock.patch("cognee.infrastructure.databases.vector.embeddings.get_embedding_engine.get_embedding_engine", return_value=mock_embedding_engine):
        
        with pytest.raises(ValueError) as exc_info:
            await _check_embedding_dimensions()
            
        assert "Embedding-dimension mismatch:" in str(exc_info.value)
        assert "stored dim 384 (bge-small-en-v1.5)" in str(exc_info.value)
        assert "query dim 1024 (text-embedding-3-large)" in str(exc_info.value)


@pytest.mark.asyncio
async def test_dimension_match_no_raise_hermes():
    mock_vector_engine = mock.MagicMock()
    mock_embedding_engine = mock.MagicMock()

    # Set matching dimensions (384)
    mock_embedding_engine.get_vector_size.return_value = 384
    mock_embedding_engine.model = "BAAI/bge-small-en-v1.5"

    mock_conn = mock.MagicMock()
    mock_conn.table_names = mock.AsyncMock(return_value=["TestCollection_text"])
    mock_vector_engine.get_connection = mock.AsyncMock(return_value=mock_conn)

    mock_collection = mock.MagicMock()
    mock_field = mock.MagicMock()
    mock_field.type = mock.MagicMock(list_size=384)
    mock_collection.schema.field.return_value = mock_field
    mock_vector_engine.get_collection = mock.AsyncMock(return_value=mock_collection)

    with mock.patch("cognee.infrastructure.databases.vector.get_vector_engine.get_vector_engine", return_value=mock_vector_engine), \
         mock.patch("cognee.infrastructure.databases.vector.embeddings.get_embedding_engine.get_embedding_engine", return_value=mock_embedding_engine):
        
        # Should not raise any ValueError
        await _check_embedding_dimensions()
