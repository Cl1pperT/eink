"""Headless frame rendering runtime for the Raspberry Pi service."""

from .config import ConfigError, RuntimeConfig, load_runtime_config
from .ee02 import EncodedEE02Frame, LandscapeRotation, decode_ee02, encode_ee02
from .esp_client import ESPClientError, ESPPullResult, ESPProtocolError, SimulatedESPClient
from .frame_server import FrameServer, running_frame_server
from .runtime import FrameRuntime, RuntimeArtifact, SourcePolicyError

__all__ = [
    "ConfigError",
    "EncodedEE02Frame",
    "ESPClientError",
    "ESPPullResult",
    "ESPProtocolError",
    "FrameRuntime",
    "FrameServer",
    "LandscapeRotation",
    "RuntimeArtifact",
    "RuntimeConfig",
    "SourcePolicyError",
    "SimulatedESPClient",
    "decode_ee02",
    "encode_ee02",
    "load_runtime_config",
    "running_frame_server",
]

__version__ = "0.1.0"
