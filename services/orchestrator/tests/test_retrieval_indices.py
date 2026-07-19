"""Task Group 2: additive per-request ``retrieval_indices`` index scope.

Focused tests only (per task 2.1): the REQUIRED absent-field regression
(byte-identical single-``legal-rag-bench`` behavior + verbatim provenance
echo), the present-field ``index=`` assertion + provenance echo, and that
``extra="forbid"`` still holds (the field is DECLARED, not silently allowed).
The search client is mocked via the shared ``client`` fixture (conftest).
Exhaustive index-string-permutation and multi-hit-merge tests are skipped.
"""

VALID_REQUEST = {
    "question": "Is the supply of legal services to a non-resident GST-free?",
    "query_id": "q-scope",
    "pipeline_config": "legal-rag-default-1.0.0",
}


def test_absent_retrieval_indices_is_byte_identical_to_today(client, mock_search):
    """REQUIRED regression: no field ⇒ single Settings index, verbatim echo.

    ``client.search`` is invoked with ``index=None`` (the client then falls
    back to its lifespan-bound ``legal-rag-bench``) and
    ``system_version.opensearch_index`` echoes ``settings.opensearch_index``
    verbatim — eval's rag_query_output v1.1.0 and the demo_ui are unaffected.
    """
    response = client.post("/query", json=VALID_REQUEST)
    assert response.status_code == 200

    # The search client received NO per-request index override.
    assert mock_search.search_calls[0]["index"] is None

    # Provenance echoes the single default index exactly as before.
    system_version = response.json()["result"]["system_version"]
    assert system_version["opensearch_index"] == "legal-rag-bench"


def test_present_retrieval_indices_scopes_search_and_echoes_the_queried_scope(
    client, mock_search
):
    """Two indices ⇒ exact comma-joined ``index=`` + provenance echoes it."""
    response = client.post(
        "/query",
        json={
            **VALID_REQUEST,
            "retrieval_indices": ["proj-abc123", "legal-rag-bench"],
        },
    )
    assert response.status_code == 200

    # The exact comma-joined multi-index string reaches the search client.
    assert mock_search.search_calls[0]["index"] == "proj-abc123,legal-rag-bench"

    # Provenance echoes the ACTUALLY-queried scope (replay stays attributable).
    system_version = response.json()["result"]["system_version"]
    assert system_version["opensearch_index"] == "proj-abc123,legal-rag-bench"


def test_single_project_index_scopes_to_that_index_only(client, mock_search):
    """Toggle-OFF shape: a one-element list scopes to ``proj-{id}`` alone."""
    response = client.post(
        "/query", json={**VALID_REQUEST, "retrieval_indices": ["proj-abc123"]}
    )
    assert response.status_code == 200
    assert mock_search.search_calls[0]["index"] == "proj-abc123"
    assert (
        response.json()["result"]["system_version"]["opensearch_index"] == "proj-abc123"
    )


def test_retrieval_indices_is_declared_and_extra_forbid_still_holds(client):
    """The field is DECLARED (accepted); a truly unknown field is still 422."""
    from app.schemas.query import QueryRequest

    # Declared: retrieval_indices validates as a real field.
    parsed = QueryRequest.model_validate(
        {**VALID_REQUEST, "retrieval_indices": ["proj-x"]}
    )
    assert parsed.retrieval_indices == ["proj-x"]

    # extra="forbid" intact: an unknown field is rejected with 422.
    response = client.post(
        "/query", json={**VALID_REQUEST, "totally_unknown_field": ["proj-x"]}
    )
    assert response.status_code == 422


def test_stream_path_also_scopes_search_and_echoes_the_queried_scope(client, mock_search):
    """Gap-fill: the SSE /query/stream path threads retrieval_indices too.

    demo_ui uses /query/stream, so the same scope must reach the search client
    and the final envelope's provenance — not just the blocking /query route.
    """
    import json

    response = client.post(
        "/query/stream",
        json={**VALID_REQUEST, "retrieval_indices": ["proj-abc123", "legal-rag-bench"]},
        headers={"Accept": "text/event-stream"},
    )
    assert response.status_code == 200

    # The comma-joined scope reached the mocked search client.
    assert mock_search.search_calls[0]["index"] == "proj-abc123,legal-rag-bench"

    # The final envelope echoes the queried scope in provenance.
    final = None
    for block in response.text.split("\n\n"):
        if block.startswith("event: final"):
            data = "\n".join(
                line.removeprefix("data: ")
                for line in block.split("\n")
                if line.startswith("data: ")
            )
            final = json.loads(data)
    assert final is not None
    assert (
        final["result"]["system_version"]["opensearch_index"]
        == "proj-abc123,legal-rag-bench"
    )
