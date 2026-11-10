from __future__ import annotations

import base64

import pytest
import republic
from bub.core.codemode.republic_bridge import decode_value, encode_value
from bub.environment import CodeFailed
from bub_monty_env import MontyEnvironment
from bub_monty_env.republic_module import STREAM_UNSUPPORTED, rewrite_republic_imports
from bub_monty_env.runner import UNSUPPORTED_HINT


@pytest.fixture
async def env(tmp_path):
    environment = MontyEnvironment(tmp_path)
    yield environment
    await environment.close()


class FakeBub:
    """Serve call_tool as Bub does: decode the arguments, encode the result."""

    def __init__(self, results: dict[str, object]) -> None:
        self.results = results
        self.calls: list[tuple[str, object]] = []

    async def __call__(self, name: str, arguments: dict) -> object:
        self.calls.append((name, decode_value(arguments)))
        return encode_value(self.results[name])


async def run(env, code: str, bub: FakeBub, tools=()) -> str:
    output: list[str] = []
    await env.run_code(code, tools=list(tools), call_tool=bub, write=output.append)
    return "".join(output)


def chat_response(text: str) -> dict:
    return {
        "text": text,
        "reasoning": "",
        "refusal": "",
        "finish_reason": "stop",
        "model": "openai:gpt-test",
        "token_usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        "message": {"$republic": "message", "role": "assistant", "content": [text]},
    }


async def test_chat_goes_through_call_tool(env) -> None:
    bub = FakeBub({"republic.chat": chat_response("pong")})
    code = (
        "import republic\n"
        "model = republic.get_model()\n"
        "reply = await model.chat('ping', temperature=0)\n"
        "print(reply.text, reply.token_usage.total_tokens, reply.message.role, reply.message.text)"
    )
    assert await run(env, code, bub) == "pong 5 assistant pong\n"
    [(name, arguments)] = bub.calls
    assert name == "republic.chat"
    assert arguments == {
        "spec": None,
        "messages": [republic.user("ping")],
        "options": {"temperature": 0},
    }


async def test_messages_and_media_cross_as_republic_values(env) -> None:
    bub = FakeBub({"republic.chat": chat_response("a chart"), "attach": "ok"})
    code = (
        "from republic import Image, get_model, system, user\n"
        "img = Image('image/png', data=b'PNG')\n"
        "print(img.data_url, isinstance(img, republic.Image))\n"
        "reply = await get_model('openai:gpt').chat([system('be brief'), user('what is it?', img)])\n"
        "history = [user('what is it?'), reply.message, republic.Message('user', ['and?'])]\n"
        "await republic.get_model('openai:gpt').chat(history)\n"
        "print(await tools.attach(image=img))"
    )
    data_url = "data:image/png;base64," + base64.b64encode(b"PNG").decode()
    assert await run(env, code, bub, tools=["attach"]) == f"{data_url} True\nok\n"
    image = republic.Image("image/png", data=b"PNG")
    assert bub.calls[0][1]["messages"] == [
        republic.system("be brief"),
        republic.user("what is it?", image),
    ]
    assert bub.calls[1][1]["messages"] == [
        republic.user("what is it?"),
        republic.assistant("a chart"),
        republic.user("and?"),
    ]
    assert bub.calls[2] == ("attach", {"image": image})


async def test_tool_results_with_media_become_republic_values(env) -> None:
    bub = FakeBub(
        {"shot": {"image": republic.Image("image/jpeg", url="https://x/y.jpg")}}
    )
    code = "r = await tools.shot()\nprint(isinstance(r['image'], republic.Image), r['image'].url)"
    assert await run(env, code, bub, tools=["shot"]) == "True https://x/y.jpg\n"


async def test_embedding_and_decision_models(env) -> None:
    bub = FakeBub(
        {
            "republic.embed": {
                "vectors": [[0.1, 0.2]],
                "model": "openai:emb",
                "token_usage": {"input_tokens": 1},
            },
            "republic.decide": {
                "answers": {"spam": {"probability": 0.9}},
                "model": "openai:dec",
                "token_usage": {"input_tokens": 1},
            },
        }
    )
    code = (
        "from republic.decisions import Noul\n"
        "e = await republic.get_embedding_model('openai:emb').embed('hi', dimensions=2)\n"
        "print(e.vector, e.vectors)\n"
        "d = await republic.get_decision_model('openai:dec').decide(\n"
        "    {'text': 'buy now'}, questions={'spam': Noul('Is it spam?')}\n"
        ")\n"
        "print(d.spam.probability, d.answers['spam'].probability, d.model)"
    )
    assert await run(env, code, bub) == "[0.1, 0.2] [[0.1, 0.2]]\n0.9 0.9 openai:dec\n"
    assert bub.calls[0] == (
        "republic.embed",
        {"spec": "openai:emb", "texts": ["hi"], "dimensions": 2},
    )
    name, arguments = bub.calls[1]
    assert name == "republic.decide"
    assert arguments["state"] == {"text": "buy now"}
    assert type(arguments["questions"]["spam"]).__name__ == "Noul"


async def test_model_errors_are_catchable(env) -> None:
    async def call_tool(name, arguments):
        raise RuntimeError("republic.chat failed: no credentials")

    code = (
        "try:\n"
        "    await republic.get_model().chat('hi')\n"
        "except Exception as e:\n"
        "    print('caught', e)"
    )
    output: list[str] = []
    await env.run_code(code, tools=[], call_tool=call_tool, write=output.append)
    assert output == ["caught republic.chat failed: no credentials\n"]


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("republic.get_model().stream('hi')", STREAM_UNSUPPORTED),
        ("async with republic.get_model() as m:\n    pass", UNSUPPORTED_HINT),
    ],
)
async def test_streaming_is_unsupported(env, code, message) -> None:
    with pytest.raises(CodeFailed) as info:
        await run(env, code, FakeBub({}))
    assert message in info.value.error


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("import republic\nx = 1", "pass\nx = 1"),
        ("import json, republic as r", "import json; r = republic"),
        ("import republic.decisions", "pass"),
        (
            "from republic import Image, user as u",
            "Image = republic.Image; u = republic.user",
        ),
        (
            "def f():\n    from republic.decisions import Noul\n    return Noul",
            "def f():\n    Noul = republic.decisions.Noul\n    return Noul",
        ),
        (
            "from republic import (\n    Image,\n    user,\n); x = '中'",
            "Image = republic.Image; user = republic.user \\\n \\\n \\\n; x = '中'",
        ),
        ("import json\nfrom os import path", "import json\nfrom os import path"),
        ("from republic import *", "from republic import *"),
        ("x = (", "x = ("),
        ("await x\nimport republic", "await x\npass"),
    ],
)
def test_rewrite_republic_imports(code, expected) -> None:
    assert rewrite_republic_imports(code) == expected


async def test_rewritten_imports_keep_traceback_lines(env) -> None:
    code = "from republic import (\n    Image,\n)\n1/0"
    with pytest.raises(CodeFailed) as info:
        await run(env, code, FakeBub({}))
    assert "line 4" in info.value.traceback
