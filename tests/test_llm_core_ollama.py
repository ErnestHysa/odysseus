"""Regression tests for native Ollama Cloud provider handling."""
import httpx

from src import llm_core


def test_detects_ollama_cloud_native_provider():
    assert llm_core._detect_provider("https://ollama.com/api") == "ollama"
    assert llm_core._detect_provider("https://ollama.com/api/chat") == "ollama"


def test_llm_call_posts_native_ollama_payload(monkeypatch):
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["url"] = url
        seen["headers"] = headers
        seen["json"] = json
        seen["timeout"] = timeout
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            request=request,
            json={"message": {"content": "OK"}, "done": True},
        )

    monkeypatch.setattr(llm_core.httpx, "post", fake_post)

    result = llm_core.llm_call(
        "https://ollama.com/api",
        "gpt-oss:120b-test",
        [{"role": "user", "content": "Say OK"}],
        temperature=0.2,
        max_tokens=7,
        headers={"Authorization": "Bearer ollama-key"},
        timeout=11,
    )

    assert result == "OK"
    assert seen["url"] == "https://ollama.com/api/chat"
    assert seen["headers"]["Authorization"] == "Bearer ollama-key"
    assert seen["json"]["stream"] is False
    # No num_ctx in the default path because the test doesn't stub
    # get_context_length, so it falls back to DEFAULT_CONTEXT and the
    # builder's safety guard skips the option (see issue #909).
    assert seen["json"]["options"] == {"temperature": 0.2, "num_predict": 7}


def test_build_ollama_payload_emits_num_ctx_when_known_and_large():
    """_build_ollama_payload passes num_ctx through when the caller
    supplies a trusted value larger than Ollama's 2048 default."""
    payload = llm_core._build_ollama_payload(
        "kimi-k2", [{"role": "user", "content": "x"}],
        temperature=0.5, max_tokens=100, num_ctx=131072,
    )
    assert payload["options"]["num_ctx"] == 131072


def test_build_ollama_payload_omits_num_ctx_at_or_below_2048():
    """Ollama's default num_ctx is 2048. Setting it lower (or equal)
    would override a user-tuned value downward, so the builder drops it."""
    for ctx in (None, 0, 512, 1024, 2048):
        payload = llm_core._build_ollama_payload(
            "m", [{"role": "user", "content": "x"}],
            temperature=0.5, max_tokens=100, num_ctx=ctx,
        )
        assert "num_ctx" not in payload.get("options", {}), (
            f"num_ctx={ctx} should not be emitted"
        )


def test_build_ollama_payload_omits_default_context_fallback():
    """get_context_length returns DEFAULT_CONTEXT (128000) when it can't
    discover the model's actual window. Emitting that as num_ctx would
    lie to Ollama for unknown models, so the builder filters it out."""
    from src.model_context import DEFAULT_CONTEXT
    payload = llm_core._build_ollama_payload(
        "unknown-llm-9001", [{"role": "user", "content": "x"}],
        temperature=0.5, max_tokens=100, num_ctx=DEFAULT_CONTEXT,
    )
    assert "num_ctx" not in payload.get("options", {})


def test_llm_call_threads_discovered_num_ctx(monkeypatch):
    """When get_context_length returns a real, large value, it ends up
    in the outgoing Ollama request as options.num_ctx (issue #909)."""
    monkeypatch.setattr(llm_core, "get_context_length",
                        lambda url, model: 32768)

    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["json"] = json
        request = httpx.Request("POST", url)
        return httpx.Response(
            200, request=request,
            json={"message": {"content": "OK"}, "done": True},
        )

    monkeypatch.setattr(llm_core.httpx, "post", fake_post)

    llm_core.llm_call(
        "https://ollama.com/api",
        "kimi-k2",
        [{"role": "user", "content": "Say OK"}],
        temperature=0.2,
        max_tokens=7,
    )

    assert seen["json"]["options"]["num_ctx"] == 32768


def test_stream_llm_threads_discovered_num_ctx(monkeypatch):
    """stream_llm goes through the same ollama branch and must also
    pass num_ctx through to the streaming request body."""
    import asyncio

    seen = {}

    def spy_build_ollama_payload(*args, **kwargs):
        seen["num_ctx"] = kwargs.get("num_ctx")
        seen["stream"] = kwargs.get("stream")
        return {
            "model": "kimi-k2",
            "messages": [{"role": "user", "content": "x"}],
            "stream": True,
        }

    monkeypatch.setattr(llm_core, "get_context_length",
                        lambda url, model: 32768)
    monkeypatch.setattr(llm_core, "_build_ollama_payload",
                        spy_build_ollama_payload)

    # Short-circuit before the actual HTTP call: host is "dead" → yields
    # an error SSE chunk and returns. The call to _build_ollama_payload
    # still happens before the host check, so we can inspect it.
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda url: True)

    async def collect():
        return [chunk async for chunk in llm_core.stream_llm(
            "https://ollama.com/api",
            "kimi-k2",
            [{"role": "user", "content": "Say OK"}],
            temperature=0.2,
            max_tokens=7,
        )]

    out = asyncio.run(collect())

    assert seen["num_ctx"] == 32768
    assert seen["stream"] is True
    assert out  # we got the SSE error chunk
