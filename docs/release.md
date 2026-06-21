# Release Checklist

Use this checklist when cutting a versioned release.

## Before Tagging

- Confirm `main` is green in CI.
- Run the local quality gate:

```bash
python -m ruff check .
python -m pytest -q
python -m compileall src tests
python -m build
```

- Run hardware-in-the-loop validation for hardware-facing changes.
- Update `pyproject.toml` version.
- Update README examples when public behavior changed.
- Review new MCP tools for schema clarity and backward compatibility.
- Confirm generated distributions in `dist/` install in a clean environment.

## Tagging

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

## Release Notes

Include:

- new MCP tools or response-shape changes
- supported probe or MCU workflow changes
- compatibility notes
- test and HIL evidence
- known limits
