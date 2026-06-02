# Static Artifact Example

This example publishes a directory that already contains static files.

```bash
bub pages add static-demo ./packages/bub-pages/examples/static-artifact/public --path /static-demo
bub pages publish static-demo
bub pages serve --host 127.0.0.1 --port 8000
```

Open:

```text
http://127.0.0.1:8000/static-demo/
```
