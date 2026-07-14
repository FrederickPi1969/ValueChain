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


def test_swagger_and_openapi_expose_auth_workflow_and_detailed_resolver_contract() -> None:
    app = FastAPI(
        title="test",
        description="Detailed acquisition API",
        docs_url="/docs",
    )
    app.state.file_api_token = ""
    app.include_router(router)
    client = TestClient(app)

    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    security = schema["components"]["securitySchemes"]
    assert security["AcquisitionApiKey"]["name"] == "X-API-Key"
    assert security["AcquisitionBearer"]["scheme"] == "bearer"
    resolve = schema["paths"]["/api/acquisition/resolve"]["post"]
    assert resolve["summary"] == "Resolve or acquire a company disclosure"
    assert "local corpus first" in resolve["description"]
    assert "202" in resolve["responses"]
    assert resolve["security"] == [
        {"AcquisitionApiKey": []},
        {"AcquisitionBearer": []},
    ]
