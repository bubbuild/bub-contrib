from __future__ import annotations

from pathlib import Path

import yaml

from bub_workflow.models import BeeTemplateInput, topological_node_ids


EXAMPLES_DIR = Path(__file__).parents[1] / "examples"


def test_example_workflows_are_valid_templates() -> None:
    paths = sorted(EXAMPLES_DIR.glob("*/workflow.yaml"))

    assert {path.parent.name for path in paths} == {"counting-relay", "werewolf"}

    for path in paths:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        template = BeeTemplateInput.model_validate(payload)

        assert topological_node_ids(template)
        assert all(node.executor == "subagent" for node in template.nodes)
        assert all(node.model is None for node in template.nodes)


def test_counting_relay_is_state_driven() -> None:
    template = _load_example("counting-relay")

    assert template.name == "counting_chat"
    assert template.input_schema["target"]["default"] == 20
    assert len(template.input_schema["participants"]["default"]) == 5
    assert len(template.nodes) == 7
    participant_nodes = [
        node for node in template.nodes if node.id.startswith("participant_")
    ]
    node = template.node_map["public_transcript"]
    prompt = node.prompt or ""
    schema = node.output_schema or {}
    properties = schema.get("properties", {})

    assert len(participant_nodes) == 5
    assert all(node.executor == "subagent" for node in participant_nodes)
    assert all(not node.depends_on for node in participant_nodes)
    assert node.executor == "subagent"
    assert node.call is None
    assert set(node.depends_on) == {f"participant_{index:02d}" for index in range(1, 6)}
    assert "not a moderated selection process" in prompt
    assert "arrival-ordered public channel" in prompt
    assert "state gate" in prompt
    assert "participants" in properties
    assert properties["participants"]["maxItems"] == 5
    assert "transcript" in properties
    assert "rejected_messages" in properties
    assert "complete" in properties
    assert "numbers" not in properties

    forbidden_fragments = [
        "bub_workflow.room",
        "count_01",
        "count_20",
        "participant_20",
        "The moderator accepts",
        "accepted_message",
        "public slot",
        "group_size",
        "Report up to",
        "consecutive integers",
        "5, 6, 7, 8",
        "9, 10, 11, 12",
        "13, 14, 15, 16",
        "17, 18, 19, 20",
    ]
    for fragment in forbidden_fragments:
        assert fragment not in prompt

    auditor = template.nodes[-1]
    assert auditor.id == "transcript_auditor"
    assert auditor.depends_on == ["public_transcript"]


def test_werewolf_example_keeps_player_roles_private() -> None:
    template = _load_example("werewolf")
    statement_nodes = [
        node
        for node in template.nodes
        if node.id.endswith("_statement")
    ]
    vote_nodes = [node for node in template.nodes if node.id.endswith("_vote")]

    assert template.name == "werewolf_ten_player"
    assert len(template.input_schema["players"]["default"]) == 10
    assert len(statement_nodes) == 10
    assert len(vote_nodes) == 10
    assert template.nodes[0].id == "moderator_setup"
    assert all(node.executor == "subagent" for node in template.nodes)
    assert all(node.allowed_tools == ["help"] for node in template.nodes)

    setup_prompt = template.node_map["moderator_setup"].prompt or ""
    assert "Dynamically assign private roles" in setup_prompt
    assert "private_envelopes for seat_01 through seat_10" in setup_prompt

    statement_ids = {f"seat_{index:02d}_statement" for index in range(1, 11)}
    vote_ids = {f"seat_{index:02d}_vote" for index in range(1, 11)}
    assert {node.id for node in statement_nodes} == statement_ids
    assert {node.id for node in vote_nodes} == vote_ids
    assert all(node.depends_on == ["moderator_setup"] for node in statement_nodes)
    assert all(
        set(node.depends_on) == {statement.id for statement in statement_nodes}
        for node in vote_nodes
    )

    combined_statement_prompt = "\n".join(node.prompt or "" for node in statement_nodes)
    assert "Your private role is" not in combined_statement_prompt
    assert "Alice: villager" not in combined_statement_prompt
    assert "Victor: werewolf" not in combined_statement_prompt
    for index in range(1, 11):
        assert (
            f"{{nodes.moderator_setup.private_envelopes.seat_{index:02d}}}"
            in combined_statement_prompt
        )

    vote_prompt = "\n".join(node.prompt or "" for node in vote_nodes)
    for index in range(1, 11):
        assert f"{{nodes.seat_{index:02d}_statement.statement}}" in vote_prompt
        assert f"{{nodes.seat_{index:02d}_statement}}" not in vote_prompt
        assert f"{{nodes.moderator_setup.private_envelopes.seat_{index:02d}}}" in vote_prompt

    day_resolution = template.node_map["day_resolution"]
    assert set(day_resolution.depends_on) == {node.id for node in vote_nodes}
    resolution_prompt = day_resolution.prompt or ""
    winner_schema = day_resolution.output_schema["properties"]["winner"]
    eliminated_schema = day_resolution.output_schema["properties"]["eliminated"]
    assert "Do not simulate future days or nights" in resolution_prompt
    assert "next_round_state" in day_resolution.output_schema["properties"]
    assert "unresolved" in winner_schema["enum"]
    assert {"type": "object", "additionalProperties": True} in eliminated_schema["anyOf"]

    endgame = template.nodes[-1]
    assert endgame.id == "endgame_resolution"
    assert endgame.depends_on == ["day_resolution"]
    endgame_prompt = endgame.prompt or ""
    endgame_winner_schema = endgame.output_schema["properties"]["winner"]
    assert "continue hosted night and day phases" in endgame_prompt
    assert "unresolved" not in endgame_winner_schema["enum"]
    assert "terminal" in endgame.output_schema["properties"]


def _load_example(name: str) -> BeeTemplateInput:
    path = EXAMPLES_DIR / name / "workflow.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return BeeTemplateInput.model_validate(payload)
