from contextlib import asynccontextmanager
import logging
from pathlib import Path
import secrets
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from minio.error import S3Error

from .config import Settings
from .models import InputError, PreviewRequest, RuleValidationRequest, PreflightRequest, validate_rules, validate_parse_options
from .bindings import preflight, validate_rule_set
from .ruleschemas import rule_schemas
from .artifact_preview import ArtifactPreviewRequest, preview_artifact
from .storage import ObjectStorage
from .tableio import preview_table

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.settings = settings or Settings.from_env()
        application.state.storage = ObjectStorage(application.state.settings)
        application.state.preview_slots = BoundedSemaphore(2)
        yield

    application = FastAPI(
        title="Data Cleaning Engine (internal)",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def authorize(request: Request, x_internal_token: str = Header(default="")) -> None:
        if not secrets.compare_digest(
            x_internal_token, request.app.state.settings.internal_token
        ):
            raise HTTPException(401, "Invalid internal token")

    @application.get("/health")
    def health():
        return {"status": "UP"}

    @application.post("/internal/preview", dependencies=[Depends(authorize)])
    def preview(payload: PreviewRequest, request: Request):
        slots = request.app.state.preview_slots
        if not slots.acquire(blocking=False):
            raise HTTPException(429, "Preview service is busy; retry shortly")
        try:
            with TemporaryDirectory(prefix="cleaning-preview-") as temporary:
                source = Path(temporary) / f"input.{payload.format}"
                request.app.state.storage.download_input(payload.objectKey, source)
                return preview_table(
                    source,
                    payload.format,
                    payload.limit,
                    payload.parseOptions,
                    payload.limits,
                )
        except InputError as exc:
            raise HTTPException(422, str(exc)) from exc
        except S3Error as exc:
            logger.warning("Preview storage failure: %s", exc.code)
            raise HTTPException(
                502, "Input object could not be read from storage"
            ) from exc
        except Exception as exc:
            logger.exception("Unexpected preview failure")
            raise HTTPException(
                422, "Input file could not be parsed; verify its format and limits"
            ) from exc
        finally:
            slots.release()

    @application.post("/internal/validate-rules", dependencies=[Depends(authorize)])
    def validate(payload: RuleValidationRequest):
        try:
            if payload.engineVersion == "v3-disk":
                normalized = validate_rule_set(payload.logicalFields, payload.rules, payload.schemaVersion)
                return {"valid": True, "normalizedRules": normalized}
            validate_rules(payload.rules, payload.engineVersion)
            return {"valid": True}
        except InputError as exc:
            raise HTTPException(422, str(exc)) from exc

    @application.post("/internal/preflight", dependencies=[Depends(authorize)])
    def validate_preflight(payload: PreflightRequest):
        try:
            fields = payload.input.fields
            parse_options = validate_parse_options(payload.input.parseOptions)
            if isinstance(fields,list):
                parsed_types = parse_options.get("fieldTypes", {})
                fields = [dict(field,logicalType=("decimal" if parsed_types.get(field.get("id")) == "number" else parsed_types.get(field.get("id"), "string" if payload.input.format == "csv" else field.get("logicalType","unknown")))) for field in fields]
            return preflight(fields, payload.logicalFields, payload.fieldBindings, payload.rules, payload.schemaVersion)
        except InputError as exc:
            raise HTTPException(422, str(exc)) from exc

    @application.get("/internal/rule-schemas/3", dependencies=[Depends(authorize)])
    def schemas():
        return rule_schemas()

    @application.post("/internal/artifact-preview", dependencies=[Depends(authorize)])
    def artifact_preview(payload: ArtifactPreviewRequest, request: Request):
        slots = request.app.state.preview_slots
        if not slots.acquire(blocking=False):
            raise HTTPException(429, "Preview service is busy; retry shortly")
        try:
            with TemporaryDirectory(prefix="cleaning-artifact-preview-") as temporary:
                source = Path(temporary) / f"artifact.{payload.format}"
                request.app.state.storage.download_artifact(payload.objectKey, source)
                return preview_artifact(source, payload.format, payload.limit)
        except InputError as exc:
            raise HTTPException(422, str(exc)) from exc
        except S3Error as exc:
            logger.warning("Artifact preview storage failure: %s", exc.code)
            raise HTTPException(502, "Task artifact could not be read from storage") from exc
        except Exception as exc:
            logger.exception("Unexpected artifact preview failure")
            raise HTTPException(
                422, "Task artifact could not be parsed; verify its format"
            ) from exc
        finally:
            slots.release()

    return application


app = create_app()
