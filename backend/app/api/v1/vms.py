from __future__ import annotations

from typing import List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

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
    """
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
