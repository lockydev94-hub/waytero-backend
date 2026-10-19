# ============================================================
# WAYTERO — ADMIN BRANCH API
# File: app/modules/admin/branch_api.py
# Doc Ref: migration 0085_branches_and_user_branch; plan B7
# Mounted at /admin/branches (read: every staff role — api/router.py)
#
# The office register. Reading it is open to every staff role because a branch
# account has to render its own name in the topbar and a super admin's switcher
# needs the list — but writing it is super-admin only: whoever can create an
# office can move their own account into it, and then every scoping rule becomes
# a rule about a row they authored.
#
# What a branch is *not*: a permission set, a currency, a price list. It is one
# city's office, and the portal filters a queue by that city.
# ============================================================

from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_super_admin
from app.modules.admin.services import branches as branch_service

router = APIRouter(tags=["Admin Branches"])


class BranchOut(BaseModel):
    id: int
    name: str
    code: str
    city_id: int
    address: Optional[str]
    is_headquarters: bool
    is_active: bool
    city_name: Optional[str]
    state_name: Optional[str]


class BranchCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    code: str = Field(..., min_length=2, max_length=20)
    city_id: int = Field(..., gt=0)
    address: Optional[str] = Field(None, max_length=255)
    is_headquarters: bool = False
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def _name_is_not_blank(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("name cannot be blank")
        return stripped

    @field_validator("code")
    @classmethod
    def _code_is_canonical(cls, v: str) -> str:
        # The database's uniqueness is case-sensitive; the switcher shows the
        # code to people who will type it either way. Normalising here keeps
        # "bbs" from becoming a second branch of the same city.
        return v.strip().upper()


class BranchUpdate(BaseModel):
    """Partial update — a field that is absent stays exactly as it is."""

    name: Optional[str] = None
    code: Optional[str] = None
    city_id: Optional[int] = Field(None, gt=0)
    address: Optional[str] = Field(None, max_length=255)
    is_headquarters: Optional[bool] = None
    is_active: Optional[bool] = None

    @field_validator("name")
    @classmethod
    def _name_when_sent(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            raise ValueError("name cannot be null — omit it to leave the name alone")
        stripped = v.strip()
        if not stripped:
            raise ValueError("name cannot be blank")
        return stripped

    @field_validator("code")
    @classmethod
    def _code_when_sent(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            raise ValueError("code cannot be null — omit it to leave the code alone")
        return v.strip().upper()

    @field_validator("city_id", "is_headquarters", "is_active")
    @classmethod
    def _not_null(cls, v):
        if v is None:
            raise ValueError("send a value or omit the field")
        return v


@router.get(
    "",
    response_model=List[BranchOut],
    summary="Every branch, head office first",
    description=(
        "The office list. Every staff role can read it: a branch account needs "
        "its own name for the topbar, a super admin needs all of them for the "
        "switcher and the branch column. Closed offices are included and marked "
        "— the register is where you reopen one."
    ),
)
async def list_branches(db: AsyncSession = Depends(get_db)) -> List[dict]:
    return await branch_service.list_branches(db)


@router.get(
    "/{branch_id}",
    response_model=BranchOut,
    summary="One branch",
    description="The row the portal filtered a queue by, for a detail page or a tooltip.",
)
async def get_branch(
    branch_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
    return await branch_service.get_branch(db, branch_id)


@router.post(
    "",
    response_model=BranchOut,
    status_code=201,
    summary="Register a branch",
    description=(
        "Open an office in a city. The city has to be an active master-data city, "
        "no other office may already hold it, and only one office in the platform "
        "may be the headquarters — the branch that owns work nobody has assigned a "
        "city to yet."
    ),
    dependencies=[Depends(require_super_admin())],
)
async def create_branch(
    payload: BranchCreate, db: AsyncSession = Depends(get_db)
) -> dict:
    return await branch_service.create_branch(
        db,
        name=payload.name,
        code=payload.code,
        city_id=payload.city_id,
        address=payload.address,
        is_headquarters=payload.is_headquarters,
        is_active=payload.is_active,
    )


@router.patch(
    "/{branch_id}",
    response_model=BranchOut,
    summary="Edit a branch",
    description=(
        "Rename an office, move it to another city, change its code, make it the "
        "headquarters, open or close it. Only the fields you send are written — "
        "leaving `is_active` out of the body does not close the branch. Closing "
        "one signs its staff out on their next request."
    ),
    dependencies=[Depends(require_super_admin())],
)
async def update_branch(
    branch_id: int, payload: BranchUpdate, db: AsyncSession = Depends(get_db)
) -> dict:
    return await branch_service.update_branch(
        db, branch_id, branch_service.clean_update(payload)
    )


@router.delete(
    "/{branch_id}",
    response_model=BranchOut,
    summary="Delete a branch that nothing points at",
    description=(
        "Refused while the office still has staff accounts or bookings — those "
        "would be orphaned from the portal that filtered by them. Move the staff "
        "and close the branch instead; deletion is for a register mistake."
    ),
    dependencies=[Depends(require_super_admin())],
)
async def delete_branch(
    branch_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
    return await branch_service.delete_branch(db, branch_id)
