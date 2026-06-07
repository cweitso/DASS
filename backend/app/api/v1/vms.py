from __future__ import annotations

from typing import List

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
    vm_ids: List[str]

@router.post("", response_model=VMCreateResponse)
def create_worker_vms(req: VMCreateRequest):
    """
    API endpoint to launch one or multiple Worker VMs at once.

    預設停用：這個端點會透過 host docker.sock 起容器，等於 RCE 級別的能力，而專案目前
    沒有任何認證機制。需要手動開 worker 時才以 DASS_VM_ADMIN_API_ENABLED=true 打開。
    """
    if not get_settings().vm_admin_api_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="VM admin API is disabled. Set DASS_VM_ADMIN_API_ENABLED=true to enable.",
        )
    if req.count <= 0:
        raise HTTPException(status_code=400, detail="Count must be greater than 0")
        
    vm_ids = vm_service.create_vms(count=req.count, instance_type=req.instance_type)
    
    return {
        "message": f"Successfully requested {len(vm_ids)} Worker VMs",
        "vm_ids": vm_ids
    }

@router.get("", response_model=List[str])
def list_worker_vms():
    """List mock active VMs."""
    return vm_service.get_active_vms()
