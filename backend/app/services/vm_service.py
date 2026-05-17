from __future__ import annotations

import logging
from typing import List
from uuid import uuid4

logger = logging.getLogger(__name__)

class VMService:
    """
    Simulates a VM management service (e.g., AWS EC2, GCP Compute Engine).
    Responsible for creating and managing Worker VMs.
    """
    
    def __init__(self):
        # In a real environment, this might maintain a connection to AWS/GCP
        self._mock_vms = []

    def create_vms(self, count: int, instance_type: str = "t3.micro") -> List[str]:
        """
        Creates multiple Worker VMs at once.
        Returns a list of created VM instance IDs.
        """
        if count <= 0:
            return []
            
        logger.info(f"Requesting creation of {count} worker VMs of type {instance_type}...")
        
        new_vm_ids = []
        for _ in range(count):
            vm_id = f"i-{uuid4().hex[:8]}"
            self._mock_vms.append(vm_id)
            new_vm_ids.append(vm_id)
            logger.info(f"✨ Worker VM created: {vm_id}")
            
        # In a real scenario, this would trigger startup scripts (user-data)
        # to install docker, pull the worker code, and run WorkerService.
            
        return new_vm_ids

    def get_active_vms(self) -> List[str]:
        return self._mock_vms.copy()

# Singleton for simplicity in simulation
vm_service = VMService()
