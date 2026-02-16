import json
from pathlib import Path

def load_config():
    """Load config.json from the project root."""
    project_root = Path(__file__).parent.parent
    config_path = project_root / 'config.json'
    
    if not config_path.exists():
        # Fallback to example if the user hasn't created their config yet
        # (though in production they must create it)
        config_path = project_root / 'config.json.example'
        
    with open(config_path, 'r') as f:
        return json.load(f)
