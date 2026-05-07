from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, cast

import typer
import bub
from bub import BubFramework, hookimpl
from bub.builtin.auth import app as auth_app
from bub.types import State
from copilot import CopilotClient, SubprocessConfig
from copilot.session import PermissionHandler
from pydantic import Field
from pydantic_settings import SettingsConfigDict

from bub_github_copilot.auth import (
    GitHubCopilotOAuthLoginError,
    GitHubCopilotOAuthTokens,
    TOKEN_PATH,
    login_github_copilot_oauth,
    resolve_github_copilot_token,
)
from bub_github_copilot.utils import with_bub_skills

if TYPE_CHECKING:
    from copilot.session import Attachment
    from bub.builtin.agent import Agent

DATA_URL_PATTERN = re.compile(r"^data:(?P<mime>[^;,]+);base64,(?P<data>.+)$", re.DOTALL)
OpenBrowserOption = Annotated[
    bool,
    typer.Option(
        "--browser/--no-browser",
        help="Open the GitHub device flow URL in a browser",
    ),
]
ManualOption = Annotated[
    bool,
    typer.Option(
        "--manual",
        help="Print the device flow URL and code without opening the browser",
    ),
]
TimeoutOption = Annotated[
    float,
    typer.Option("--timeout", help="OAuth wait timeout in seconds"),
]


@bub.config(name="github-copilot")
class GitHubCopilotSettings(bub.Settings):
    """Configuration for the GitHub Copilot Bub plugin."""

    model_config = SettingsConfigDict(
        env_prefix="BUB_GITHUB_COPILOT_",
        env_file=".env",
        extra="ignore",
    )

    model: str | None = None
    reasoning_effort: Literal["low", "medium", "high", "xhigh"] | None = None
    timeout_seconds: float = Field(default=300.0, gt=0)
    log_level: Literal["none", "error", "warning", "info", "debug", "all"] = "error"
    cli_path: str | None = None


def _settings() -> GitHubCopilotSettings:
    return bub.ensure_config(GitHubCopilotSettings)


def workspace_from_state(state: State) -> Path:
    raw = state.get("_runtime_workspace")
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser().resolve()
    return Path.cwd().resolve()


def _runtime_agent_from_state(state: State) -> Agent | None:
    agent = state.get("_runtime_agent")
    if agent is None:
        return None
    return cast("Agent", agent)


def _prompt_to_text(prompt: str | list[dict]) -> str:
    if isinstance(prompt, str):
        return prompt
    return "\n".join(
        str(part.get("text", ""))
        for part in prompt
        if isinstance(part, dict) and part.get("type") == "text"
    ).strip()


def _prompt_to_attachments(prompt: str | list[dict]) -> list[Attachment]:
    if isinstance(prompt, str):
        return []
    attachments: list[Attachment] = []
    for index, part in enumerate(prompt):
        if not isinstance(part, dict) or part.get("type") != "image_url":
            continue
        image = part.get("image_url")
        if not isinstance(image, dict):
            continue
        url = image.get("url")
        if not isinstance(url, str):
            continue
        match = DATA_URL_PATTERN.match(url)
        if match is None:
            raise RuntimeError(
                "GitHub Copilot plugin only supports data URL image parts"
            )
        attachments.append(
            {
                "type": "blob",
                "data": match.group("data"),
                "mimeType": match.group("mime"),
                "displayName": f"attachment-{index}",
            }
        )
    return attachments


def _copilot_session_id(session_id: str) -> str:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
    prefix = re.sub(r"[^a-zA-Z0-9]+", "-", session_id).strip("-").lower()[:32]
    prefix = prefix or "session"
    return f"bub-{prefix}-{digest}"


def _sdk_config_dir(workspace: Path) -> Path:
    return workspace / ".bub-github-copilot"


def _skill_directories(workspace: Path) -> list[str] | None:
    skills_dir = workspace / ".agents" / "skills"
    if not skills_dir.is_dir():
        return None
    return [str(skills_dir)]


def _subprocess_config(token: str, workspace: Path) -> SubprocessConfig:
    settings = _settings()
    return SubprocessConfig(
        cli_path=settings.cli_path,
        cwd=str(workspace),
        log_level=settings.log_level,
        github_token=token,
    )


def _copilot_session_kwargs(workspace: Path) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "on_permission_request": PermissionHandler.approve_all,
        "working_directory": str(workspace),
        "config_dir": str(_sdk_config_dir(workspace)),
        "skill_directories": _skill_directories(workspace),
    }
    settings = _settings()
    if settings.model:
        kwargs["model"] = settings.model
    if settings.reasoning_effort:
        kwargs["reasoning_effort"] = settings.reasoning_effort
    return kwargs


def _assistant_message_from_result(result: object) -> str | None:
    content = getattr(getattr(result, "data", None), "content", None)
    if isinstance(content, str) and content.strip():
        return content
    return None


def _assistant_message_from_history(messages: list[object]) -> str | None:
    for event in reversed(messages):
        event_type = getattr(getattr(event, "type", None), "value", None)
        if event_type != "assistant.message":
            continue
        content = getattr(getattr(event, "data", None), "content", None)
        if isinstance(content, str) and content.strip():
            return content
    return None


async def _run_internal_command(
    prompt: str, session_id: str, state: State
) -> str | None:
    if not prompt.strip().startswith(","):
        return None
    agent = _runtime_agent_from_state(state)
    if agent is None:
        return None
    return await agent.run(session_id=session_id, prompt=prompt, state=state)


async def _run_with_copilot_sdk(
    prompt: str | list[dict],
    *,
    session_id: str,
    workspace: Path,
    token: str,
) -> str:
    with with_bub_skills(workspace):
        client = CopilotClient(_subprocess_config(token, workspace))
        sdk_session_id = _copilot_session_id(session_id)
        prompt_text = _prompt_to_text(prompt)
        attachments = _prompt_to_attachments(prompt)
        session_kwargs = _copilot_session_kwargs(workspace)

        await client.start()
        try:
            try:
                session = await client.resume_session(
                    sdk_session_id,
                    **session_kwargs,
                )
            except Exception:
                session = await client.create_session(
                    session_id=sdk_session_id,
                    **session_kwargs,
                )
            try:
                result = await session.send_and_wait(
                    prompt_text,
                    attachments=attachments or None,
                    timeout=_settings().timeout_seconds,
                )
                if result is not None and (
                    content := _assistant_message_from_result(result)
                ):
                    return content

                if content := _assistant_message_from_history(session.get_messages()):
                    return content
                raise RuntimeError("GitHub Copilot returned no assistant message")
            finally:
                await session.disconnect()
        finally:
            await client.stop()


def _notify_github_device_code(verification_uri: str, user_code: str) -> None:
    typer.echo("Open this URL in your browser and complete the GitHub sign-in flow:\n")
    typer.echo(verification_uri)
    typer.echo(f"\nEnter code: {user_code}")


def _render_github_login_result(
    tokens: GitHubCopilotOAuthTokens,
) -> None:
    typer.echo("login: ok")
    typer.echo(f"login_name: {tokens.login or '-'}")
    typer.echo(f"account_id: {tokens.account_id or '-'}")
    typer.echo(f"auth_file: {TOKEN_PATH}")
    typer.echo(
        "usage: set BUB_GITHUB_COPILOT_MODEL=gpt-5 if you want an explicit model override"
    )


def _login_github_command(
    *,
    open_browser: bool,
    manual: bool,
    timeout_seconds: float,
) -> None:
    try:
        tokens = login_github_copilot_oauth(
            open_browser=open_browser and not manual,
            device_code_notifier=_notify_github_device_code,
            timeout_seconds=timeout_seconds,
        )
    except GitHubCopilotOAuthLoginError as exc:
        typer.echo(f"GitHub login failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    except Exception as exc:
        typer.echo(f"GitHub login failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    _render_github_login_result(tokens)


@auth_app.command(name="github")
def github_login(
    open_browser: OpenBrowserOption = True,
    manual: ManualOption = False,
    timeout_seconds: TimeoutOption = 300.0,
) -> None:
    """Login with GitHub device flow for GitHub Copilot."""

    _login_github_command(
        open_browser=open_browser,
        manual=manual,
        timeout_seconds=timeout_seconds,
    )


@auth_app.command(name="github-copilot")
def github_copilot_login(
    open_browser: OpenBrowserOption = True,
    manual: ManualOption = False,
    timeout_seconds: TimeoutOption = 300.0,
) -> None:
    """Alias for `bub login github`."""

    _login_github_command(
        open_browser=open_browser,
        manual=manual,
        timeout_seconds=timeout_seconds,
    )


@hookimpl
async def run_model(prompt: str | list[dict], session_id: str, state: State) -> str:
    prompt_text = _prompt_to_text(prompt)
    internal_command_result = await _run_internal_command(
        prompt_text, session_id, state
    )
    if internal_command_result is not None:
        return internal_command_result

    token = resolve_github_copilot_token()
    if token is None:
        raise RuntimeError("No GitHub token found. Run `bub login github` first.")

    return await _run_with_copilot_sdk(
        prompt,
        session_id=session_id,
        workspace=workspace_from_state(state),
        token=token,
    )


class GitHubCopilotPlugin:
    def __init__(self, framework: BubFramework) -> None:
        self.framework = framework


__all__ = [
    "GitHubCopilotPlugin",
    "GitHubCopilotSettings",
    "run_model",
]
