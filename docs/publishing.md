# Publishing to PyPI

The Python distribution and primary command are named `agenturm`. The former `agent-goechala` and `agent-observatory` commands remain compatibility aliases.

The rename does not publish a new package automatically. Register the pending publisher below before tagging the first Agenturm release. `agenturm` is a new PyPI distribution and needs its own Trusted Publisher registration.

## First release

1. On PyPI, open **Account settings → Publishing** and add a pending GitHub Actions publisher:
   - PyPI project: `agenturm`
   - Owner: `abhishekash`
   - Repository: `agenturm`
   - Workflow: `publish.yml`
   - Environment: `pypi`
2. Choose a version and update `pyproject.toml` and `uv.lock`. After those
   changes and the workflow are on `main`, push a matching, unused tag. For
   example, after setting the version to `0.2.0`:

   ```bash
   git tag v0.2.0
   git push origin v0.2.0
   ```

The workflow checks that the tag matches `pyproject.toml`, runs the test suite, builds and validates the wheel and source archive, then publishes both with PyPI Trusted Publishing. No long-lived PyPI token is stored in GitHub. PyPI creates the project from the pending publisher on its first successful upload.

## Later releases

Update `version` in `pyproject.toml` and `uv.lock`, merge the release changes to `main`, then tag and push the matching version. PyPI does not allow replacing a version after upload; publish a new version to correct a release.
