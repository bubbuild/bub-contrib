import asyncio
import sys
import os

# Add src to path for direct testing without install
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from bub_kapy.plugin import KapyModel

async def test_kapy_model():
    model = KapyModel()
    results = []
    async for chunk in model.run_model("Test prompt"):
        results.append(chunk)
    
    output = "".join(results)
    print(f"Output: {output}")
    assert "Gudu" in output
    print("Smoke test passed!")

if __name__ == "__main__":
    asyncio.run(test_kapy_model())
