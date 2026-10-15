# Publishing to PyPI

The Python distribution is named `agent-observatory-cli`; the installed command remains `agent-observatory`.

## First release

1. On PyPI, open **Account settings → Publishing** and add a pending GitHub Actions publisher:
   - PyPI project: `agent-observatory-cli`
   - Owner: `abhishekash`
   - Repository: `agent-observatory`
   - Workflow: `publish.yml`
   - Environment: `pypi`
2. Push the version tag after the workflow changes are on `main`:

   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```

The workflow checks that the tag matches `pyproject.toml`, runs the test suite, builds and validates the wheel and source archive, then publishes both with PyPI Trusted Publishing. No long-lived PyPI token is stored in GitHub. PyPI creates the project from the pending publisher on its first successful upload.

## Later releases

Update `version` in `pyproject.toml`, merge the release changes to `main`, then tag and push the matching version, for example `v0.1.1`. PyPI does not allow replacing a version after upload; publish a new version to correct a release.
