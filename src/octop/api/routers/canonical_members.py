"""Thin current-organization membership adapters for the canonical authority."""

from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field

from octop.api.routers.canonical_auth import _access, _CanonicalAuthRoute, _error
from octop.api.routers.canonical_identity import _BEARER
from octop.infra.errors import ErrorCode, MossworkError
from octop.infra.identity.organization_members import (
    CanonicalOrganizationMemberService,
    DepartmentUpdate,
)

_NO_STORE = {"Cache-Control": "no-store"}


class OrganizationMemberResponse(BaseModel):
    account_id: str | None
    organization_id: str
    member_id: str
    username: str
    display_name: str | None
    role: str
    status: str
    membership_revision: int
    created_at: int = Field(description="Original canonical creation time in Unix milliseconds.")
    legacy_id: int | None
    department_id: str | None


class OrganizationMembersResponse(BaseModel):
    members: list[OrganizationMemberResponse]


class OrganizationDepartmentResponse(BaseModel):
    department_id: str
    organization_id: str
    parent_id: str | None
    name: str
    created_at: int
    updated_at: int
    user_count: int


class OrganizationDepartmentsResponse(BaseModel):
    departments: list[OrganizationDepartmentResponse]


class DepartmentDetailsResponse(OrganizationDepartmentResponse):
    external_id: str | None = Field(description="Original external directory ID; no new identity.")


class DepartmentMutationResponse(BaseModel):
    department: DepartmentDetailsResponse


class DepartmentDeletionResponse(BaseModel):
    ok: Literal[True] = True


class CreateDepartmentBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1)
    parent_id: str | None = Field(default=None, description="Null creates an organization root.")
    external_id: str | None = None


class UpdateDepartmentBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str | None = None
    parent_id: str | None = Field(default=None, description="Omit to retain; null moves to root.")
    external_id: str | None = Field(default=None, description="Omit to retain; null clears the ID.")

    def changes(self) -> DepartmentUpdate:
        result: DepartmentUpdate = {}
        if "name" in self.model_fields_set:
            if self.name is None:
                raise MossworkError.localized(
                    ErrorCode.IDENTITY_REQUEST_INVALID, status=400, details={"field": "department"}
                )
            result["name"] = self.name
        if "parent_id" in self.model_fields_set:
            result["parent_id"] = self.parent_id
        if "external_id" in self.model_fields_set:
            result["external_id"] = self.external_id
        return result


class AddOrganizationMemberBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: str = Field(min_length=1, max_length=64, repr=False)


class OrganizationMemberAdditionResponse(BaseModel):
    member: OrganizationMemberResponse
    created: bool


class SetMemberPasswordBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    password: str = Field(repr=False, description="Replacement password; must be nonempty.")


class MemberPasswordResponse(BaseModel):
    ok: Literal[True] = True


class OrganizationInvitationResponse(BaseModel):
    invitation_id: str
    organization_id: str
    code: str = Field(repr=False)
    status: Literal["pending", "used", "revoked"]
    initial_credit_units: int | float = Field(
        description="Original stored invitation credit units."
    )
    legacy_initial_quota_usd: float | None
    used_by_member_id: str | None = Field(description="Original canonical member ID, when used.")
    created_at: int
    used_at: int | None


class OrganizationInvitationsResponse(BaseModel):
    items: list[OrganizationInvitationResponse]
    total: int


def build_canonical_members_router(service: CanonicalOrganizationMemberService) -> APIRouter:
    router = APIRouter(route_class=_CanonicalAuthRoute)

    @router.get(
        "/invitations",
        response_model=OrganizationInvitationsResponse,
        summary="List the selected organization's retained canonical invitations",
        description=(
            "Requires current organization administrator membership and signed admin:users scope. "
            "Department administrators are refused. Reads only the existing canonical invitation "
            "records, ordered by original creation time and ID descending. Limit defaults to 20 "
            "and is clamped to 1–100; negative offset becomes zero. Invitation codes are returned "
            "only to this authorized caller with no-store. Stored credit/quota values are facts; "
            "the response preserves the original invitation metadata."
        ),
    )
    async def list_invitations(
        request: Request,
        status: Literal["pending", "used", "revoked"] | None = None,
        limit: int = 20,
        offset: int = 0,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> JSONResponse:
        try:
            result = await service.list_invitations(
                _access(credentials), status=status, limit=limit, offset=offset
            )
            return JSONResponse(
                OrganizationInvitationsResponse(
                    items=[OrganizationInvitationResponse(**asdict(item)) for item in result.items],
                    total=result.total,
                ).model_dump(),
                headers=_NO_STORE,
            )
        except MossworkError as error:
            return _error(request, error)

    @router.post(
        "/members/{member_id}/password",
        response_model=MemberPasswordResponse,
        summary="Reset an authorized canonical member's password",
        description=(
            "Requires current selected-organization administrator membership, signed admin:users "
            "scope and exclusive canonical writer. Department administrators may target only "
            "user/dept_admin members in their current subtree. Updates only the original "
            "password hash and timestamp; leaves local-auth flags, issued tokens and "
            "memberships unchanged. A final refusal may follow a committed write and does not authorize "
            "an automatic retry."
        ),
    )
    async def set_member_password(
        request: Request,
        member_id: str,
        body: SetMemberPasswordBody,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> JSONResponse:
        try:
            await service.set_member_password(_access(credentials), member_id, body.password)
            return JSONResponse(MemberPasswordResponse().model_dump(), headers=_NO_STORE)
        except MossworkError as error:
            return _error(request, error)

    @router.get(
        "/members",
        response_model=OrganizationMembersResponse,
        summary="List the selected organization's canonical members",
        description=(
            "Requires current administrator membership and signed admin:users scope. "
            "Department administrators see only user and department-administrator members "
            "in their current department subtree. "
            "Returns membership facts without passwords, phone numbers, model keys or balances."
        ),
    )
    async def list_members(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> JSONResponse:
        try:
            members = await service.list_members(_access(credentials))
            return JSONResponse(
                OrganizationMembersResponse(
                    members=[OrganizationMemberResponse(**asdict(member)) for member in members]
                ).model_dump(),
                headers=_NO_STORE,
            )
        except MossworkError as error:
            return _error(request, error)

    @router.get(
        "/departments",
        response_model=OrganizationDepartmentsResponse,
        summary="List the current organization's authorized department directory",
        description=(
            "Requires current administrator membership and signed admin:users scope. "
            "Department administrators see only their current department subtree. "
            "Counts include only members visible to the caller. Returns directory facts, "
            "without granting model, native runtime or member-management effects."
        ),
    )
    async def list_departments(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> JSONResponse:
        try:
            departments = await service.list_departments(_access(credentials))
            return JSONResponse(
                OrganizationDepartmentsResponse(
                    departments=[
                        OrganizationDepartmentResponse(**asdict(department))
                        for department in departments
                    ]
                ).model_dump(),
                headers=_NO_STORE,
            )
        except MossworkError as error:
            return _error(request, error)

    @router.post(
        "/departments",
        response_model=DepartmentMutationResponse,
        summary="Create a department in the selected canonical organization",
        description=(
            "Requires current administrator membership, signed admin:users scope and exclusive "
            "canonical writer. Department administrators may create only within their current "
            "subtree. Organization selection cannot come from the body. Sibling names and "
            "external directory IDs remain unique. A final writer refusal can follow a committed "
            "write; it does not imply rollback or authorize an automatic retry."
        ),
    )
    async def create_department(
        request: Request,
        body: CreateDepartmentBody,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> JSONResponse:
        try:
            result = await service.create_department(
                _access(credentials),
                body.name,
                parent_id=body.parent_id,
                external_id=body.external_id,
            )
            return JSONResponse(
                DepartmentMutationResponse(
                    department=DepartmentDetailsResponse(**asdict(result))
                ).model_dump(),
                headers=_NO_STORE,
            )
        except MossworkError as error:
            return _error(request, error)

    @router.patch(
        "/departments/{department_id}",
        response_model=DepartmentMutationResponse,
        summary="Update an authorized canonical department",
        description=(
            "Requires current signed admin:users authority and exclusive canonical writer. "
            "Both the existing department and a selected new parent must be in the caller's "
            "current scope. Cycles, foreign parents and duplicate sibling names or external IDs "
            "are refused. Omitted fields stay unchanged; null parent/external ID clears it."
        ),
    )
    async def update_department(
        request: Request,
        department_id: str,
        body: UpdateDepartmentBody,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> JSONResponse:
        try:
            result = await service.update_department(
                _access(credentials), department_id, body.changes()
            )
            return JSONResponse(
                DepartmentMutationResponse(
                    department=DepartmentDetailsResponse(**asdict(result))
                ).model_dump(),
                headers=_NO_STORE,
            )
        except MossworkError as error:
            return _error(request, error)

    @router.delete(
        "/departments/{department_id}",
        response_model=DepartmentDeletionResponse,
        summary="Delete an empty authorized canonical department",
        description=(
            "Requires current signed admin:users authority and exclusive canonical writer. "
            "Departments with any child or assigned member are refused, including disabled "
            "members. This operation does not move/delete members, stop native writers or "
            "grant permission to delete production data outside this department."
        ),
    )
    async def delete_department(
        request: Request,
        department_id: str,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> JSONResponse:
        try:
            await service.delete_department(_access(credentials), department_id)
            return JSONResponse(DepartmentDeletionResponse().model_dump(), headers=_NO_STORE)
        except MossworkError as error:
            return _error(request, error)

    @router.post(
        "/members",
        response_model=OrganizationMemberAdditionResponse,
        summary="Add an existing human account to the selected organization",
        description=(
            "Requires the current organization administrator, signed admin:users scope and "
            "exclusive canonical writer. The phone must identify an existing active account. "
            "Other organization memberships and credentials are preserved. An existing selected-"
            "organization membership is returned unchanged. Does not register a new account."
        ),
    )
    async def add_member(
        request: Request,
        body: AddOrganizationMemberBody,
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> JSONResponse:
        try:
            result = await service.add_registered_account(_access(credentials), body.phone)
            return JSONResponse(
                OrganizationMemberAdditionResponse(
                    member=OrganizationMemberResponse(**asdict(result.member)),
                    created=result.created,
                ).model_dump(),
                headers=_NO_STORE,
            )
        except MossworkError as error:
            return _error(request, error)

    return router
