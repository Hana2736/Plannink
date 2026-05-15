import json
import os
from pathlib import Path

# Environment variable overrides — useful for Docker deployments where you
# don't want to bake host-specific values into a mounted config.json.
# Each entry maps an env var name to a tuple of config key path components.
_ENV_OVERRIDES = {
    'PLANNINK_CONTROL_HOST': ('server', 'control_host'),
    'PLANNINK_SHM_PATH':     ('paths', 'shm_state'),
    'PLANNINK_API_SECRET':   ('api_secret',),
    'PLANNINK_GEM_BIND':     ('gem', 'bind'),
    'PLANNINK_GEM_PORT':     ('gem', 'port'),
    'PLANNINK_POOL_INGEST_TOKEN': ('pool_ingest', 'token'),
}

def load_config():
    """Load config.json from the project root, then apply any env var overrides."""
    project_root = Path(__file__).parent.parent
    config_path = project_root / 'config.json'

    if not config_path.exists():
        # Fallback to example if the user hasn't created their config yet
        # (though in production they must create it)
        config_path = project_root / 'config.json.example'

    with open(config_path, 'r') as f:
        config = json.load(f)

    for env_key, path in _ENV_OVERRIDES.items():
        val = os.environ.get(env_key)
        if val is not None:
            node = config
            for key in path[:-1]:
                node = node.setdefault(key, {})
            node[path[-1]] = val

    return config
