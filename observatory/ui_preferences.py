"""Small allowlisted cockpit preferences; never save search or scope identities."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

VIEWS = ('agents', 'activity', 'mcp', 'traces', 'host', 'metrics', 'findings')


def safe_preferences(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    if value.get('theme') in ('Current', 'Bright', 'Dark', 'Solarized'):
        result['theme'] = value['theme']
    if value.get('view') in VIEWS:
        result['view'] = value['view']
    for key in ('reviews', 'prompts'):
        if isinstance(value.get(key), bool):
            result[key] = value[key]
    if isinstance(value.get('errors'), list):
        result['errors'] = [page for page in ('activity', 'mcp') if page in value['errors']]
    if result.get('prompts') and 'activity' in result.get('errors', []):
        result['errors'].remove('activity')
    return result


class Preferences:
    def __init__(self, path=None, enabled=True):
        self.path = Path(path) if path else Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'agenturm' / 'ui.json'
        self.enabled = enabled and os.environ.get('AGENTURM_UI_PREFERENCES', '1').lower() not in {'0', 'false', 'off'}

    def load(self):
        if not self.enabled:
            return {}
        try:
            if self.path.stat().st_size > 16384:
                return {}
            return safe_preferences(json.loads(self.path.read_text(encoding='utf-8')))
        except (OSError, ValueError, RecursionError):
            return {}

    def save(self, value):
        if not self.enabled:
            return
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.NamedTemporaryFile(mode='w', dir=self.path.parent, encoding='utf-8', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(safe_preferences(value), stream, indent=2)
                stream.write('\n')
            temporary.replace(self.path)
        except OSError:
            # A read-only configuration directory must not break monitoring.
            pass
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
