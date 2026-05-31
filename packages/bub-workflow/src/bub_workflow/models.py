from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BeeNodeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1)
    title: str | None = None
    description: str = ""
    prompt: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    executor: Literal["subagent", "function"] = "subagent"
    call: str | None = None
    model: str | None = None
    allowed_tools: list[str] | None = None
    allowed_skills: list[str] | None = None
    output_schema: dict[str, Any] | None = None
    features: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_executor(self) -> BeeNodeInput:
        if self.executor == "function" and not self.call:
            raise ValueError("function executor requires call")
        if self.executor == "subagent" and not self.prompt:
            raise ValueError("subagent executor requires prompt")
        return self


class BeeTemplateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    description: str = ""
    skill: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, dict[str, Any]] = Field(default_factory=dict)
    nodes: list[BeeNodeInput] = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_graph(self) -> BeeTemplateInput:
        ids = [node.id for node in self.nodes]
        duplicates = sorted({node_id for node_id in ids if ids.count(node_id) > 1})
        if duplicates:
            raise ValueError(f"bee node ids must be unique: {', '.join(duplicates)}")
        known = set(ids)
        for node in self.nodes:
            missing = sorted(set(node.depends_on) - known)
            if missing:
                names = ", ".join(missing)
                raise ValueError(f"bee node '{node.id}' depends on unknown node(s): {names}")
        _topological_nodes(self.nodes)
        return self

    @property
    def node_map(self) -> dict[str, BeeNodeInput]:
        return {node.id: node for node in self.nodes}


class BeeTemplateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    inputs: dict[str, Any] = Field(default_factory=dict)
    description: str = ""
    skill: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, dict[str, Any]] = Field(default_factory=dict)
    nodes: list[BeeNodeInput] | None = None

    @model_validator(mode="after")
    def validate_source_shape(self) -> BeeTemplateRequest:
        if self.nodes is not None:
            return self

        provided = []
        if self.description:
            provided.append("description")
        if self.skill:
            provided.append("skill")
        if self.config:
            provided.append("config")
        if self.input_schema:
            provided.append("input_schema")
        if provided:
            names = ", ".join(provided)
            raise ValueError(f"reusable template request cannot define: {names}")
        return self

    @property
    def is_inline(self) -> bool:
        return self.nodes is not None

    def inline_template(self) -> BeeTemplateInput:
        if self.nodes is None:
            raise ValueError("template nodes are required for an inline template")
        return BeeTemplateInput(
            name=self.name,
            description=self.description,
            skill=self.skill,
            config=self.config,
            input_schema=self.input_schema,
            nodes=self.nodes,
        )


class BeeStartInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template: BeeTemplateRequest
    brief: str = Field(..., min_length=1)
    run_id: str | None = None
    execute: bool = True


class BeeNodeProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    description: str = ""
    depends_on: list[str] = Field(default_factory=list)
    executor: str
    call: str | None = None
    prompt: str | None = None
    model: str | None = None
    allowed_tools: list[str] | None = None
    allowed_skills: list[str] | None = None
    output_schema: dict[str, Any] | None = None
    features: list[str] = Field(default_factory=list)
    status: Literal["pending", "running", "completed", "failed", "skipped"] = "pending"
    output: Any | None = None
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class BeeProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    template_name: str
    template_source: str | None = None
    description: str = ""
    brief: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    skill: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending", "running", "completed", "failed"] = "pending"
    nodes: dict[str, BeeNodeProjection]
    result: Any | None = None
    error: str | None = None
    created_at: str
    updated_at: str
    finished_at: str | None = None


def _topological_nodes(nodes: list[BeeNodeInput]) -> list[str]:
    node_map = {node.id: node for node in nodes}
    visiting: set[str] = set()
    visited: set[str] = set()
    ordered: list[str] = []

    def visit(node_id: str) -> None:
        if node_id in visited:
            return
        if node_id in visiting:
            raise ValueError(f"bee template contains a dependency cycle at node: {node_id}")
        visiting.add(node_id)
        for dependency_id in node_map[node_id].depends_on:
            visit(dependency_id)
        visiting.remove(node_id)
        visited.add(node_id)
        ordered.append(node_id)

    for node in nodes:
        visit(node.id)
    return ordered


def topological_node_ids(template: BeeTemplateInput) -> list[str]:
    return _topological_nodes(template.nodes)
