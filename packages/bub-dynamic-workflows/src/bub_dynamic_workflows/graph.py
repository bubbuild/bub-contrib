from __future__ import annotations

from collections import defaultdict, deque

from bub_dynamic_workflows.errors import WorkflowSpecError
from bub_dynamic_workflows.spec import WorkflowSpec


def topological_node_ids(spec: WorkflowSpec) -> list[str]:
    in_degree = {node.id: 0 for node in spec.nodes}
    children: dict[str, list[str]] = defaultdict(list)

    for node in spec.nodes:
        for dependency_id in node.depends_on:
            children[dependency_id].append(node.id)
            in_degree[node.id] += 1

    queue = deque(node_id for node_id, degree in in_degree.items() if degree == 0)
    seen: list[str] = []
    while queue:
        node_id = queue.popleft()
        seen.append(node_id)
        for child_id in children[node_id]:
            in_degree[child_id] -= 1
            if in_degree[child_id] == 0:
                queue.append(child_id)

    if len(seen) != len(spec.nodes):
        cyclic = sorted(node_id for node_id, degree in in_degree.items() if degree > 0)
        raise WorkflowSpecError(f"workflow graph contains a cycle: {', '.join(cyclic)}")
    return seen


def validate_acyclic_graph(spec: WorkflowSpec) -> None:
    topological_node_ids(spec)
