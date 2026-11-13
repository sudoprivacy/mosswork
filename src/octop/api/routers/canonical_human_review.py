"""Bearer-authenticated capture/recovery of an original human review document.

Trusted product composition owns the one mount and supplies current human
grants, registered transport, audit and canonical writer. This leaf creates
neither a public HTML route nor an Agent, native session or page credential.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from octop.api.common.canonical_identity import canonical_authenticated_request
from octop.api.routers.canonical_auth import _error
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity.request_principal import CanonicalAuthenticatedRequest
from octop.infra.native_host.human_review import HumanReviewReader
from octop.infra.sudograph.protocol import ReviewDocument, SudographError

if TYPE_CHECKING:
    from octop.infra.db.repos.retained_review import RetainedHumanReviewDocumentRepo
    from octop.infra.identity.projection import CanonicalRequestIdentityResolver
    from octop.infra.identity.write_authority import CanonicalWriterAdmission

_PATH = "/ontology-projects/{project_id}/reviews/{review_id}/v1"
_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


class CapturedHumanReviewResponse(BaseModel):
    record: dict[str, Any] = Field(
        description=(
            "Original sudograph-served-review/v1 record: authority, model, source/resource "
            "binding, revision, context_digest, endpoint, html_sha256 and record_sha256. "
            "These checksums are not access grants."
        )
    )
    html: str = Field(description="Unchanged compiler HTML persisted with that original record.")


class HumanReviewReaderResolver(Protocol):
    async def __call__(
        self,
        principal: CanonicalAuthenticatedRequest,
        *,
        project_id: str,
        endpoint: str,
    ) -> HumanReviewReader: ...


class CapturedHumanReviewPageResolver(Protocol):
    async def read_page(
        self,
        principal: CanonicalAuthenticatedRequest,
        *,
        project_id: str,
        endpoint: str,
        documents: RetainedHumanReviewDocumentRepo,
        record_sha256: str,
        request: object,
    ) -> dict[str, Any]:
        """Restore the original record, check current grants and return review-v2.

        The SAME configured resolver supplies this capability. It must validate
        the stored document/digest and original renderer body with the domain
        codec, current full-model/source/projection grants, transport and audit.
        Its final canonical checks must preserve 401/403 outside domain error
        sanitization. The leaf performs no unrelated await after it returns.
        """
        ...


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("Invalid JSON number")


def _unavailable() -> MossworkError:
    return MossworkError.localized(
        ErrorCode.INTERNAL_ERROR, status=503, details={"dependency": "canonical_human_review"}
    )


def build_canonical_human_review_router(
    resolve_reader: HumanReviewReaderResolver,
    *,
    requests: CanonicalRequestIdentityResolver,
    documents: RetainedHumanReviewDocumentRepo,
    writer: CanonicalWriterAdmission,
) -> APIRouter:
    """Bind trusted references; the product root installs this exact tuple once."""
    if not callable(resolve_reader) or not callable(getattr(writer, "assert_current_writer", None)):
        raise ValueError("Require configured human review providers")
    expected = (resolve_reader, requests, documents, writer)
    router = APIRouter()

    def mounted(request: Request) -> None:
        current = getattr(request.app.state, "canonical_human_review_ports", None)
        if (
            type(current) is not tuple
            or len(current) != len(expected)
            or any(
                actual is not original for actual, original in zip(current, expected, strict=True)
            )
            or getattr(request.app.state, "canonical_requests", None) is not requests
        ):
            raise _unavailable()

    async def document(
        request: Request, project_id: str, review_id: str, *, capture: bool
    ) -> JSONResponse:
        try:
            mounted(request)
            principal = await canonical_authenticated_request(request)
            mounted(request)
            if (
                request.query_params
                or re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", project_id) is None
                or re.fullmatch(r"[a-f0-9]{32}", review_id) is None
            ):
                raise MossworkError.localized(ErrorCode.IDENTITY_REQUEST_INVALID, status=400)
            # Neither method accepts a body, including an alternate selector or
            # credential. Stop at the first byte instead of buffering a payload.
            async for chunk in request.stream():
                if chunk:
                    raise MossworkError.localized(ErrorCode.IDENTITY_REQUEST_INVALID, status=400)
            mounted(request)
            endpoint = "/review/" + review_id + "/v1"
            reader = await resolve_reader(principal, project_id=project_id, endpoint=endpoint)
            mounted(request)
            # Preserve the canonical 401/403 outcome after the resolver wait,
            # before the domain client sanitizes provider failures.
            await principal.revalidate()
            mounted(request)
            if (
                type(reader) is not HumanReviewReader
                or reader._human.principal is not principal
                or reader._project.project_id != project_id
                or reader.binding.resource_ref != project_id
                or reader._project.organization_id != principal.identity.snapshot.organization_id
            ):
                raise PermissionError
            if capture:
                result = await reader.capture_review(endpoint, documents, writer=writer)
            else:
                result = await reader.restore_review(endpoint, documents)
            # No unrelated await after the reader's final current-grant check.
            mounted(request)
            if (
                type(result) is not ReviewDocument
                or result.binding != reader.binding
                or result.endpoint != endpoint
            ):
                raise _unavailable()
            return JSONResponse({"record": result.record(), "html": result.html}, headers=_HEADERS)
        except asyncio.CancelledError:
            raise
        except PermissionError:
            error = MossworkError.localized(ErrorCode.FORBIDDEN, status=403)
        except SudographError as caught:
            error = (
                MossworkError.localized(ErrorCode.FORBIDDEN, status=403)
                if caught.code == "ACCESS_DENIED"
                else _unavailable()
            )
        except MossworkError as caught:
            # Preserve canonical authentication statuses without reflecting a
            # provider's private exception text, details, path or credentials.
            code = {
                400: ErrorCode.IDENTITY_REQUEST_INVALID,
                401: ErrorCode.AUTH_FAILED,
                403: ErrorCode.FORBIDDEN,
            }.get(caught.status)
            error = MossworkError.localized(code, status=caught.status) if code else _unavailable()
        except Exception:
            error = _unavailable()
        response = _error(request, error)
        response.headers.update(_HEADERS)
        return response

    @router.post(
        _PATH,
        summary="Capture an authorized original review document",
        description=(
            "Requires canonical human authentication and independent current model/source/"
            "projection review grants. Persists the exact compiler HTML and revision/digest "
            "before responding; creates no Agent or native session."
        ),
        response_model=CapturedHumanReviewResponse,
    )
    async def capture(request: Request, project_id: str, review_id: str) -> JSONResponse:
        return await document(request, project_id, review_id, capture=True)

    @router.get(
        _PATH,
        summary="Restore the same original review document",
        description=(
            "Restores the original persisted HTML and record under fresh human review grants. "
            "Does not recapture the document or substitute the latest revision/digest."
        ),
        response_model=CapturedHumanReviewResponse,
    )
    async def restore(request: Request, project_id: str, review_id: str) -> JSONResponse:
        return await document(request, project_id, review_id, capture=False)

    @router.post(
        _PATH + "/page",
        summary="Read records for the same captured review document",
        openapi_extra={
            "parameters": [
                {
                    "name": "If-Match",
                    "in": "header",
                    "required": True,
                    "description": "Quoted record_sha256 from the displayed stored document; not a grant.",
                    "schema": {"type": "string", "pattern": '^"[a-f0-9]{64}"$'},
                }
            ],
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "required": ["view", "operation", "params"],
                            "additionalProperties": False,
                            "properties": {
                                "view": {"type": "string", "description": "Original resource_ref."},
                                "operation": {"type": "string", "enum": ["page"]},
                                "params": {
                                    "type": "object",
                                    "description": "Original revision, context_digest, source id, offset and limit.",
                                },
                            },
                        }
                    }
                },
            },
            "responses": {
                "200": {
                    "description": "Authorized records at the captured pointer.",
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["result"],
                                "properties": {"result": {"type": "object"}},
                            }
                        }
                    },
                },
                "409": {"description": "The original captured pointer no longer matches."},
            },
        },
    )
    async def page(request: Request, project_id: str, review_id: str) -> JSONResponse:
        try:
            mounted(request)
            principal = await canonical_authenticated_request(request)
            mounted(request)
            matches = request.headers.getlist("if-match")
            content_types = request.headers.getlist("content-type")
            if (
                request.query_params
                or re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", project_id) is None
                or re.fullmatch(r"[a-f0-9]{32}", review_id) is None
                or len(matches) != 1
                or re.fullmatch(r'"[a-f0-9]{64}"', matches[0]) is None
                or len(content_types) != 1
                or content_types[0].split(";", 1)[0].strip().lower() != "application/json"
            ):
                raise ValueError("Invalid captured review request")
            payload = bytearray()
            async for chunk in request.stream():
                if len(payload) + len(chunk) > 65536:
                    raise ValueError("Review read request exceeds the existing SDK bound")
                payload.extend(chunk)
            body = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_json_object,
                parse_constant=_invalid_constant,
            )
            if type(body) is not dict:
                raise ValueError("Expected original renderer request object")
            mounted(request)
            await principal.revalidate()
            mounted(request)
            read_page = getattr(resolve_reader, "read_page", None)
            if not callable(read_page):
                raise _unavailable()
            # This is the one held configured object, not document resolution,
            # an upstream URL from the frame or a second factory/provider slot.
            result = await read_page(
                principal,
                project_id=project_id,
                endpoint="/review/" + review_id + "/v1",
                documents=documents,
                record_sha256=matches[0][1:-1],
                request=body,
            )
            if type(result) is not dict or "result" not in result:
                raise _unavailable()
            # Own the result; the provider validates the existing domain
            # envelope and finishes all asynchronous current checks itself.
            result = copy.deepcopy(result)
            mounted(request)
            # The provider finishes canonical and graph/source/projection
            # admission after its waits; release without another await that
            # could invalidate those checks. No callback/result grants access.
            return JSONResponse({"result": result["result"]}, headers=_HEADERS)
        except asyncio.CancelledError:
            raise
        except PermissionError:
            error = MossworkError.localized(ErrorCode.FORBIDDEN, status=403)
        except ValueError:
            error = MossworkError.localized(ErrorCode.IDENTITY_REQUEST_INVALID, status=400)
        except SudographError as caught:
            status = (
                403
                if caught.code == "ACCESS_DENIED"
                else 409
                if caught.code in {"CONTEXT_STALE", "REVISION_UNKNOWN", "SOURCE_SCHEMA_CHANGED"}
                else 400
                if caught.code in {"READ_CONTRACT", "TARGET_UNKNOWN", "CONTEXT_TARGET_UNKNOWN"}
                else 503
            )
            error = (
                MossworkError.localized(
                    ErrorCode.FORBIDDEN if status == 403 else ErrorCode.IDENTITY_REQUEST_INVALID,
                    status=status,
                )
                if status != 503
                else _unavailable()
            )
        except MossworkError as caught:
            code = {
                400: ErrorCode.IDENTITY_REQUEST_INVALID,
                401: ErrorCode.AUTH_FAILED,
                403: ErrorCode.FORBIDDEN,
                409: ErrorCode.IDENTITY_REQUEST_INVALID,
            }.get(caught.status)
            error = MossworkError.localized(code, status=caught.status) if code else _unavailable()
        except Exception:
            error = _unavailable()
        response = _error(request, error)
        response.headers.update(_HEADERS)
        return response

    return router
