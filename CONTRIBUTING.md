# Contributing

Thanks for helping improve Agent Observatory. For bugs and feature ideas, open a GitHub issue with the expected behavior and a minimal reproduction when possible.

## Local development

- Python 3.11 or newer
- Node.js 18 or newer for the JavaScript client
- uv recommended for isolated CLI installation

```bash
uv tool install --editable .
python -m unittest discover -s tests -v
node --check clients/javascript/index.mjs
```

Keep changes focused, document user-facing options, and avoid committing local databases, session captures, credentials, or real customer telemetry. Add or update tests for behavioral changes. CI runs the Python suite on Python 3.11–3.14 and checks the JavaScript module syntax on Node.js 22 and 24.
