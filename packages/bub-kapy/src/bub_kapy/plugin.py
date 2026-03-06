from bub import hookimpl
from bub.types import State

@hookimpl
async def run_model(prompt: str, session_id: str, state: State) -> str:
    return (
        f"Kapybara (bub-kapy) received your prompt: {prompt}\n"
        "Gudu... 🫧 This is a cyber-hybrid bubble! 🐾"
    )
