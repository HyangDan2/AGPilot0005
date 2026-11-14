import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app

@pytest.mark.asyncio
async def test_demo_tools_api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/v1/demo/tools")
        assert r.status_code == 200
        data = r.json()
        assert "tools" in data
        tool_names = [t["name"] for t in data["tools"]]
        assert "system.echo" in tool_names
        assert "system.get_datetime" in tool_names
        assert "math.calculate" in tool_names
        assert "document.get_outline" in tool_names

@pytest.mark.asyncio
async def test_demo_connect_validation():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Missing model/url
        r = await client.post("/api/v1/demo/connect", json={"mode": "direct"})
        assert r.status_code == 400

        # Gateway mode check
        r2 = await client.post("/api/v1/demo/connect", json={"mode": "gateway"})
        assert r2.status_code == 200
        data2 = r2.json()
        assert data2["ready"] is False
        assert "AI Gateway" in data2["message"]

@pytest.mark.asyncio
async def test_demo_run_agent_flow():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Agent mode run with mock model
        r = await client.post(
            "/api/v1/demo/run",
            json={
                "mode": "agent",
                "model_mode": "direct",
                "base_url": "mock",
                "model": "mock-model",
                "api_key": "mock_key",
                "message": "오늘 날짜 알려줘",
                "stream": True,
            },
        )
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        content = ""
        async for chunk in r.aiter_text():
            content += chunk
            if "event: run.completed" in content:
                break
        assert "event: run.started" in content
        assert "event: run.completed" in content

@pytest.mark.asyncio
async def test_demo_run_invalid_api_key_error():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Agent mode run with invalid key to live OpenAI endpoint
        r = await client.post(
            "/api/v1/demo/run",
            json={
                "mode": "agent",
                "model_mode": "direct",
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-4o",
                "api_key": "invalid_key",
                "message": "안녕",
                "stream": True,
            },
        )
        assert r.status_code == 200
        content = ""
        async for chunk in r.aiter_text():
            content += chunk
        assert "event: run.failed" in content
        assert "Model Provider Error (401)" in content

@pytest.mark.asyncio
async def test_demo_run_chat_mode():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/v1/demo/run",
            json={
                "mode": "chat",
                "model_mode": "direct",
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-4o",
                "api_key": "test_key",
                "message": "안녕 반가워",
                "stream": True,
            },
        )
        assert r.status_code == 200
        content = ""
        async for chunk in r.aiter_text():
            content += chunk
        assert "event: run.started" in content

@pytest.mark.asyncio
async def test_demo_frontend_serving():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Root serve or redirect
        r0 = await client.get("/", follow_redirects=False)
        if r0.status_code == 200:
            assert "text/html" in r0.headers.get("content-type", "")
        else:
            assert r0.status_code == 307
            assert r0.headers["location"] in ("/demo/", "demo/")

        # 2. /demo redirect to /demo/
        r1 = await client.get("/demo", follow_redirects=False)
        assert r1.status_code == 307
        assert r1.headers["location"] == "/demo/"

        # 3. Direct /demo/
        r = await client.get("/demo/")
        assert r.status_code == 200
        assert "AGPilot0005 — Standalone Agent Demo Console" in r.text

        # 4. Favicon
        r_fav = await client.get("/favicon.ico")
        assert r_fav.status_code == 200


@pytest.mark.asyncio
async def test_agent_dynamic_runtime_config_api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Get current config
        r = await client.get("/api/v1/config/agent")
        assert r.status_code == 200
        data = r.json()
        assert "max_tool_calls" in data
        assert "max_agent_steps" in data
        assert "run_timeout_seconds" in data
        assert data["max_tool_calls"] == 16
        assert data["max_agent_steps"] == 20
        assert data["run_timeout_seconds"] == 180.0

        # 2. Patch config
        r_patch = await client.patch("/api/v1/config/agent", json={"max_tool_calls": 24, "max_agent_steps": 30, "run_timeout_seconds": 240.0})
        assert r_patch.status_code == 200
        patched = r_patch.json()
        assert patched["max_tool_calls"] == 24
        assert patched["max_agent_steps"] == 30
        assert patched["run_timeout_seconds"] == 240.0

        # 3. Reset config
        r_reset = await client.post("/api/v1/config/agent/reset")
        assert r_reset.status_code == 200
        reset_data = r_reset.json()
        assert reset_data["max_tool_calls"] == 16
        assert reset_data["max_agent_steps"] == 20
        assert reset_data["run_timeout_seconds"] == 180.0
