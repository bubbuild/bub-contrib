from typing import AsyncIterator

class KapyModel:
    async def run_model(self, prompt: str, **kwargs) -> AsyncIterator[str]:
        yield f"Kapybara (bub-kapy) received your prompt: {prompt}\n"
        yield "Gudu... 🫧 This is a cyber-hybrid bubble! 🐾"
