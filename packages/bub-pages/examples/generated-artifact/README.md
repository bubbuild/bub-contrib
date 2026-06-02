# Generated Artifact Example

This example keeps the project files separate from the artifact directory.
The build command writes `dist/`, and `bub-pages` publishes only that artifact.

```bash
bub pages add generated-demo ./packages/bub-pages/examples/generated-artifact/dist \
  --build-dir ./packages/bub-pages/examples/generated-artifact/project \
  --build "python build.py" \
  --path /generated-demo
bub pages publish generated-demo
bub pages serve --host 127.0.0.1 --port 8000
```

Open:

```text
http://127.0.0.1:8000/generated-demo/
```
