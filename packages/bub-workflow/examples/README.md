# bub-workflow examples

This directory contains reusable workflow template bundles.

- `counting-relay/workflow.yaml`: an open public counting transcript with five participants, channel ordering, rejected stale messages, and an audit node.

Use either bundle by loading the explicit template path:

```python
params = {
    "run_id": "counting-demo",
    "brief": "Run the counting relay.",
    "template": {
        "name": "packages/bub-workflow/examples/counting-relay",
        "inputs": {"target": 20},
    },
}
```
