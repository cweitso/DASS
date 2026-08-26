from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.core.config import get_settings
from app.services.vm_service import vm_service

router = APIRouter(prefix="/vms", tags=["vms"])


class VMCreateRequest(BaseModel):
    count: int = 1
    instance_type: str = "t3.micro"


class VMCreateResponse(BaseModel):
    message: str
    vm_ids: list[str]


@router.post("", response_model=VMCreateResponse)
def create_worker_vms(req: VMCreateRequest):
    """Start worker containers by hand.

    Disabled by default: this starts containers through the host Docker socket,
    which is remote code execution on an API that has no authentication. The
    autoscaler calls VMService directly and is unaffected.
    """
    if not get_settings().vm_admin_api_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="VM admin API is disabled. Set DASS_VM_ADMIN_API_ENABLED=true to enable.",
        )
    if req.count <= 0:
        raise HTTPException(status_code=400, detail="Count must be greater than 0")

    vm_ids = vm_service.create_vms(count=req.count, instance_type=req.instance_type)
    return VMCreateResponse(
        message=f"Successfully requested {len(vm_ids)} Worker VMs", vm_ids=vm_ids
    )


@router.get("", response_model=list[str])
def list_worker_vms():
    return vm_service.get_active_vms()
