import os
from dataclasses import dataclass, field
from pathlib import Path
from .transfers import TransferSettings


@dataclass
class Settings:
    state: Path = field(default_factory=lambda: Path(os.getenv('STATE_PATH', '/state')))
    media: Path = field(default_factory=lambda: Path(os.getenv('MEDIA_PATH', '/media')))
    media_host: str = os.getenv('MEDIA_HOST_PREFIX', '')
    released: Path = field(default_factory=lambda: Path(os.getenv('RELEASED_PATH', '/media/released')))
    arm_media: str = os.getenv('ARM_MEDIA_PREFIX', '/home/arm/media')
    arm_url: str = os.getenv('ARM_URL', 'http://arm-rippers:8080')
    arm_token: str = field(default_factory=lambda: os.getenv('ARM_BEARER_TOKEN', ''), repr=False)
    drive: str = os.getenv('ARM_DRIVE', '/dev/sr0')
    tv_min_length_seconds: int = field(default_factory=lambda: int(os.getenv('TV_MIN_LENGTH_SECONDS', '900')))
    camera: str = os.getenv('CAMERA_DEVICE', '/dev/video0')
    width: int = int(os.getenv('CAMERA_WIDTH', '3840'))
    height: int = int(os.getenv('CAMERA_HEIGHT', '2160'))
    rotation: int = int(os.getenv('CAMERA_ROTATION', '180'))
    roi: str = os.getenv('CAMERA_ROI', '0.15,0.1,0.85,0.9')
    sharpness: float = float(os.getenv('CAMERA_SHARPNESS', '60'))
    settle: float = float(os.getenv('CALIBRATION_SETTLE_SECONDS', '5'))
    ollama: str = os.getenv('OLLAMA_URL', 'http://ollama:11434')
    models: tuple = tuple(os.getenv('LOCAL_VISION_MODELS', 'qwen3-vl:30b-a3b-instruct-q4_K_M,qwen2.5vl:7b-q8_0').split(','))
    model_timeout: float = float(os.getenv('MODEL_TIMEOUT_SECONDS', '600'))
    external_url: str = os.getenv('OPENAI_BASE_URL', 'https://api.openai.com/v1')
    external_model: str = os.getenv('OPENAI_VISION_MODEL', 'gpt-6-astra')
    key_file: str = os.getenv('OPENAI_KEY_FILE', '')
    external_key: str = field(default_factory=lambda: os.getenv('OPENAI_API_KEY', ''), repr=False)
    spending_limit: float = float(os.getenv('FALLBACK_SPENDING_LIMIT_USD') or '0')
    fallback_limit: int = int(os.getenv('FALLBACK_MAX_CALLS', '0'))
    user: str = os.getenv('HELPER_USER', 'operator')
    password_file: str = os.getenv('HELPER_PASSWORD_FILE', '/run/secrets/gui_password')
    transfer: TransferSettings | None = None

    def __post_init__(self):
        if self.transfer is None:
            self.transfer = TransferSettings.from_env(self.state, self.released, self.detention)
        if self.tv_min_length_seconds < 0:
            raise ValueError('TV_MIN_LENGTH_SECONDS must be a non-negative number of seconds')

    @property
    def detention(self):
        return self.media / 'detention'
