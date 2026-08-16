import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_CONFIG_DIR=Path(os.getenv('ANYAICAM_CONFIG_DIR','/etc/anyaicam'))
DEFAULT_STATE_DIR=Path(os.getenv('ANYAICAM_STATE_DIR','/var/lib/anyaicam'))
DEFAULT_LOG_DIR=Path(os.getenv('ANYAICAM_LOG_DIR','/var/log/anyaicam'))


@dataclass
class AgentConfig:
    cloud_id: str=''
    portal_url: str='http://127.0.0.1:8000'
    mode: str='development'
    checkin_seconds: int=60
    discovery_networks: list[str]|None=None
    camera_capacity: int=16
    software_version: str='0.1.0'
    recording_path: str='/var/lib/anyaicam/recordings'
    config_dir: str=str(DEFAULT_CONFIG_DIR)
    state_dir: str=str(DEFAULT_STATE_DIR)
    log_dir: str=str(DEFAULT_LOG_DIR)

    # RDM-2 (device-side integration, Group 2A): the manifest
    # target/channel values this device presents to UpdateStateMachine.
    # Not per-camera/per-customer -- these describe the appliance
    # software itself. Defaults are not invented: 'anyaicam-appliance'
    # matches every RDM-1 test manifest's target value, and 'stable'
    # matches UpdateStateMachine's own constructor default (Group 6).
    update_target: str='anyaicam-appliance'
    update_channel: str='stable'

    # RDM-2 (device-side integration, Group 2G): how often service.py's
    # periodic pull path (check_for_source_update()) calls the already-
    # existing UpdateStateMachine.check_and_install(). Deliberately a
    # SEPARATE, much slower cadence than checkin_seconds (the normal
    # heartbeat/camera/command poll interval) -- checking for a new
    # software update does not need, and must not run at, the same
    # frequency as routine operational polling.
    update_check_interval_seconds: int=900

    def __post_init__(self): self.discovery_networks=self.discovery_networks or []

    @property
    def credential_file(self): return Path(self.state_dir)/'credential.json'
    @property
    def queue_file(self): return Path(self.state_dir)/'offline_queue.db'
    @property
    def cameras_file(self): return Path(self.state_dir)/'cameras.json'
    @property
    def live_relay_commands_file(self): return Path(self.state_dir)/'live_relay_commands.json'

    # RDM-1 (Remote Device Management): additive-only properties, same style
    # as the ones above. Update state lives under state_dir (mutable
    # runtime state, like credential_file/queue_file); the trusted public
    # key lives under config_dir (provisioned, read-only trust material,
    # like agent.json).
    @property
    def updates_dir(self): return Path(self.state_dir)/'updates'
    @property
    def update_versions_dir(self): return self.updates_dir/'versions'
    @property
    def update_staging_dir(self): return self.updates_dir/'staging'
    @property
    def update_history_file(self): return self.updates_dir/'update_history.db'
    @property
    def pending_validation_file(self): return self.updates_dir/'pending_validation.json'
    @property
    def current_version_pointer_file(self): return self.updates_dir/'current_version.txt'
    @property
    def trusted_public_key_file(self): return Path(self.config_dir)/'trusted_signing_key.pem'

    @classmethod
    def load(cls,path: str|Path|None=None):
        path=Path(path or os.getenv('ANYAICAM_CONFIG_FILE',DEFAULT_CONFIG_DIR/'agent.json')); data={}
        if path.exists(): data=json.loads(path.read_text(encoding='utf-8'))
        aliases={'cloud_id':'ANYAICAM_CLOUD_ID','portal_url':'ANYAICAM_PORTAL_URL','mode':'ANYAICAM_AGENT_MODE','checkin_seconds':'ANYAICAM_CHECKIN_SECONDS','camera_capacity':'ANYAICAM_CAMERA_CAPACITY','recording_path':'ANYAICAM_RECORDING_PATH','update_target':'ANYAICAM_UPDATE_TARGET','update_channel':'ANYAICAM_UPDATE_CHANNEL','update_check_interval_seconds':'ANYAICAM_UPDATE_CHECK_INTERVAL_SECONDS'}
        for key,environment in aliases.items():
            if os.getenv(environment) is not None: data[key]=int(os.environ[environment]) if key in {'checkin_seconds','camera_capacity','update_check_interval_seconds'} else os.environ[environment]
        return cls(**{key:value for key,value in data.items() if key in cls.__dataclass_fields__})

    def save(self,path: str|Path|None=None):
        path=Path(path or Path(self.config_dir)/'agent.json'); path.parent.mkdir(parents=True,exist_ok=True); temporary=path.with_suffix('.tmp'); temporary.write_text(json.dumps(asdict(self),indent=2),encoding='utf-8'); os.chmod(temporary,0o600); temporary.replace(path); os.chmod(path,0o600)


def load_credential(config: AgentConfig) -> dict|None:
    try: return json.loads(config.credential_file.read_text(encoding='utf-8'))
    except (OSError,json.JSONDecodeError): return None


def save_credential(config: AgentConfig,value: dict):
    config.credential_file.parent.mkdir(parents=True,exist_ok=True); temporary=config.credential_file.with_suffix('.tmp'); temporary.write_text(json.dumps(value),encoding='utf-8'); os.chmod(temporary,0o600); temporary.replace(config.credential_file); os.chmod(config.credential_file,0o600)
