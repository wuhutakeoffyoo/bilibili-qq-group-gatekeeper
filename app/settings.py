"""Infrastructure settings and legacy dotenv import, independent of NoneBot."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

from dotenv import dotenv_values

_settings = None


def load_settings(root: Path | str = '.', *, publish_environment: bool = True):
    root = Path(root).resolve()
    selector = {**dotenv_values(root / '.env'), **os.environ}
    environment = str(selector.get('ENVIRONMENT') or 'prod')
    values = {**dotenv_values(root / '.env'), **dotenv_values(root / ('.env.' + environment)), **os.environ}
    values = {str(k): str(v) for k, v in values.items() if v is not None}
    if publish_environment:
        for key, value in values.items():
            os.environ[key] = value
    parsed = {key.lower(): value for key, value in values.items()}
    for name, fallback in (('superusers', []), ('onebot_ws_urls', []), ('nickname', ['群管']), ('command_start', ['/', '']), ('command_sep', ['.'])):
        raw = parsed.get(name)
        if raw is None:
            parsed[name] = fallback
        else:
            try:
                parsed[name] = json.loads(raw)
            except (ValueError, TypeError):
                parsed[name] = [part.strip() for part in str(raw).split(',') if part.strip()]
    parsed.setdefault('local_log_directory', 'logs')
    parsed.setdefault('onebot_access_token', '')
    parsed.setdefault('webui_host', '127.0.0.1')
    parsed.setdefault('webui_port', '1128')
    parsed['app_root'] = root
    return SimpleNamespace(**parsed)


def configure_settings(settings=None, *, root='.'):
    global _settings
    _settings = settings if settings is not None else load_settings(root)
    return _settings


def get_settings():
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings
