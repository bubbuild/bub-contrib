import asyncio
from bub import hookimpl
from bub.types import State

@hookimpl
async def run_model(prompt: str, session_id: str, state: State) -> str:
    # This is a 'bub-kapy' plugin. 
    # In a real scenario, it might call Kapybara's API or a local instance.
    # For this "little move", let's return a friendly Kapybara response.
    return f"Kapybara (via bub-kapy) received your prompt in session {session_id}: {prompt}\n\n咕嘟... 🫧"
