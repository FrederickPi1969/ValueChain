from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from valuechain.acquisition_resolver_api import router


def test_schema_endpoint_documents_unified_parameters_and_source_names() -> None:
    app = FastAPI()
    app.state.file_api_token = ""
    app.include_router(router)
    client = TestClient(app)

    response = client.get("/api/acquisition/schema")

    assert response.status_code == 200
    payload = response.json()
    assert "company" in payload["request_parameters"]["properties"]
    assert "document_type" in payload["request_parameters"]["properties"]
    sources = {item["source_id"]: item for item in payload["sources"]}
    assert sources["sec_edgar"]["mappings"][0]["source_names"][0] == "10-K"
    assert sources["opendart"]["fallback_mode"] == "on_demand"
    assert sources["unternehmensregister"]["fallback_mode"] == "authorized_import_only"
