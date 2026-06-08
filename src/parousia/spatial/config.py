from dataclasses import dataclass
from typing import List


@dataclass
class SpatialConfig:
    """Configuration for spatial components"""
    chromium_path: str = "/usr/bin/chromium-browser"
    profile_dir: str = "/var/lib/parousia/browsers"
    idle_timeout_seconds: int = 300
    max_instances: int = 10
    launch_args: List[str] = None
    
    def __post_init__(self):
        if self.launch_args is None:
            self.launch_args = []