from __future__ import annotations

import asyncio
import concurrent.futures
import importlib
import inspect
import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any

try:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
except Exception:
    ClientSession = None
    StdioServerParameters = None
    stdio_client = None


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _mcp_config_path() -> Path:
    configured = os.environ.get("JASTER_MCP_CONFIG", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return _project_root() / "mcp.json"


class PersistentSession:
    def __init__(self, name: str, server_config: dict[str, Any]) -> None:
        self.name = name
        self.config = server_config
        self._connect_lock = asyncio.Lock()
        self._call_lock = asyncio.Lock()
        self._session: ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._ready_event = asyncio.Event()
        self._stop_event = asyncio.Event()

    async def _run_session(self) -> None:
        if ClientSession is None or stdio_client is None or StdioServerParameters is None:
            raise RuntimeError("MCP Python SDK is not installed. Please install `mcp`.")

        command = self.config.get("command")
        args = self.config.get("args", [])
        env = {**os.environ, **(self.config.get("env") or {})}
        async with stdio_client(StdioServerParameters(command=command, args=args, env=env)) as (read, write):
            async with ClientSession(read, write) as session:
                self._session = session
                await session.initialize()
                self._ready_event.set()
                await self._stop_event.wait()

    async def ensure_connected(self) -> None:
        async with self._connect_lock:
            if self._session is not None:
                return
            self._stop_event.clear()
            self._ready_event.clear()
            self._task = asyncio.create_task(self._run_session())
            ready_task = asyncio.create_task(self._ready_event.wait())
            done, _ = await asyncio.wait({ready_task, self._task}, return_when=asyncio.FIRST_COMPLETED)
            if self._task in done:
                try:
                    self._task.result()
                except Exception as exc:
                    raise RuntimeError(f"Failed to start MCP session {self.name}: {exc}") from exc

    async def close(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except Exception:
                self._task.cancel()
        self._task = None
        self._session = None
        self._ready_event.clear()

    async def list_tools(self) -> list[str]:
        try:
            await self.ensure_connected()
            assert self._session is not None
            async with self._call_lock:
                tools_result = await self._session.list_tools()
            return [tool.name for tool in getattr(tools_result, "tools", []) or []]
        except Exception:
            return []

    async def get_tools_detailed(self) -> list[dict[str, Any]]:
        try:
            await self.ensure_connected()
            assert self._session is not None
            async with self._call_lock:
                tools_result = await self._session.list_tools()
            detailed: list[dict[str, Any]] = []
            for tool in getattr(tools_result, "tools", []) or []:
                detailed.append(
                    {
                        "name": tool.name,
                        "description": getattr(tool, "description", ""),
                        "inputSchema": getattr(tool, "inputSchema", {}) or {},
                    }
                )
            return detailed
        except Exception:
            return []

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        try:
            await self.ensure_connected()
            assert self._session is not None
            async with self._call_lock:
                result = await self._session.call_tool(tool_name, arguments=arguments)
        except Exception as exc:
            return json.dumps(
                {
                    "success": False,
                    "error_type": "MCP_CALL_ERROR",
                    "error": f"MCP call failed: {exc}",
                },
                ensure_ascii=False,
            )

        if getattr(result, "isError", False):
            message = _extract_result_text(result) or json.dumps(getattr(result, "structuredContent", {}), ensure_ascii=False)
            return json.dumps(
                {
                    "success": False,
                    "error_type": "MCP_TOOL_ERROR",
                    "error": message or "Tool returned an MCP error.",
                },
                ensure_ascii=False,
            )

        text = _extract_result_text(result)
        if text:
            return text
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return json.dumps(structured, ensure_ascii=False)
        return json.dumps(
            {
                "success": False,
                "error_type": "EMPTY_RESULT",
                "error": f"Tool {tool_name} returned empty content.",
            },
            ensure_ascii=False,
        )


class LocalSession:
    def __init__(self, name: str, server_config: dict[str, Any]) -> None:
        self.name = name
        self.config = server_config
        self._tools: dict[str, Any] = {}
        self._loaded = False

    async def ensure_connected(self) -> None:
        if self._loaded:
            return
        module_name = _extract_local_module_name(self.config)
        if not module_name:
            raise RuntimeError(f"Local MCP session {self.name} is missing a Python module target.")
        module = importlib.import_module(module_name)
        mcp = getattr(module, "mcp", None)
        tool_manager = getattr(mcp, "_tool_manager", None)
        tools = getattr(tool_manager, "_tools", None)
        if not isinstance(tools, dict):
            raise RuntimeError(f"Local MCP session {self.name} could not load registered tools.")
        self._tools = dict(tools)
        self._loaded = True

    async def close(self) -> None:
        self._tools.clear()
        self._loaded = False

    async def list_tools(self) -> list[str]:
        try:
            await self.ensure_connected()
            return list(self._tools.keys())
        except Exception:
            return []

    async def get_tools_detailed(self) -> list[dict[str, Any]]:
        try:
            await self.ensure_connected()
        except Exception:
            return []
        detailed: list[dict[str, Any]] = []
        for name, tool in self._tools.items():
            detailed.append(
                {
                    "name": name,
                    "description": getattr(tool, "description", "") or "",
                    "inputSchema": getattr(tool, "parameters", {}) or {},
                }
            )
        return detailed

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        try:
            await self.ensure_connected()
        except Exception as exc:
            return json.dumps(
                {
                    "success": False,
                    "error_type": "MCP_CALL_ERROR",
                    "error": f"Local MCP session failed to load: {exc}",
                },
                ensure_ascii=False,
            )

        tool = self._tools.get(tool_name)
        if tool is None:
            return json.dumps(
                {
                    "success": False,
                    "error_type": "MISSING_TOOL",
                    "error": f"Tool {tool_name} was not found in local MCP session {self.name}.",
                },
                ensure_ascii=False,
            )

        fn = getattr(tool, "fn", None)
        if fn is None:
            return json.dumps(
                {
                    "success": False,
                    "error_type": "MISSING_HANDLER",
                    "error": f"Tool {tool_name} does not have a callable handler.",
                },
                ensure_ascii=False,
            )

        try:
            if inspect.iscoroutinefunction(fn):
                result = await fn(**arguments)
            else:
                result = fn(**arguments)
        except Exception as exc:
            return json.dumps(
                {
                    "success": False,
                    "error_type": "MCP_TOOL_ERROR",
                    "error": f"Local MCP tool {tool_name} failed: {exc}",
                },
                ensure_ascii=False,
            )

        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False)


def _extract_result_text(result: Any) -> str:
    content = getattr(result, "content", None) or []
    for item in content:
        if hasattr(item, "text"):
            return str(item.text)
    return ""


def _extract_local_module_name(server_config: dict[str, Any]) -> str:
    args = [str(item) for item in server_config.get("args", []) or []]
    for index, item in enumerate(args[:-1]):
        if item == "-m":
            return args[index + 1]
    return ""


def _should_use_local_session(server_config: dict[str, Any]) -> bool:
    return _extract_local_module_name(server_config) == "jaster.mcp.mcp_service"


def _load_mcp_config() -> dict[str, Any]:
    config_path = _mcp_config_path()
    if not config_path.exists():
        return {}
    return json.loads(config_path.read_text(encoding="utf-8"))


def _local_only_sessions() -> dict[str, LocalSession]:
    payload = _load_mcp_config()
    servers = payload.get("mcpServers") or {}
    if not servers:
        return {}
    sessions: dict[str, LocalSession] = {}
    for name, cfg in servers.items():
        if cfg.get("type") != "stdio" or not _should_use_local_session(cfg):
            return {}
        sessions[name] = LocalSession(name, cfg)
    return sessions


class _LocalRuntime:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._startup_lock = threading.Lock()
        self._queue: queue.Queue[tuple[Any, concurrent.futures.Future] | None] = queue.Queue()

    def ensure_worker(self) -> None:
        with self._startup_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run_worker, daemon=True)
            self._thread.start()

    def _run_worker(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        while True:
            item = self._queue.get()
            if item is None:
                break
            coro, future = item
            if future.cancelled():
                continue
            try:
                result = loop.run_until_complete(coro)
            except Exception as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)
        asyncio.set_event_loop(None)
        loop.close()

    def run(self, coro: Any, *, timeout: float = 30.0) -> Any:
        self.ensure_worker()
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._queue.put((coro, future))
        return future.result(timeout=timeout)


class _Runtime:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._sessions: dict[str, PersistentSession] = {}
        self._sessions_initialized = False
        self._startup_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._docs_cache = ""
        self._docs_cache_at = 0.0

    def ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._startup_lock:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return self._loop
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._run_loop, daemon=True)
            self._thread.start()
            return self._loop

    def _run_loop(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro: Any, *, timeout: float = 30.0) -> Any:
        loop = self.ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout)

    async def initialize_sessions(self) -> None:
        if self._sessions_initialized:
            return
        config_path = _mcp_config_path()
        payload: dict[str, Any] = {}
        if config_path.exists():
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        for name, cfg in (payload.get("mcpServers") or {}).items():
            if cfg.get("type") == "stdio":
                if _should_use_local_session(cfg):
                    self._sessions[name] = LocalSession(name, cfg)
                else:
                    self._sessions[name] = PersistentSession(name, cfg)
        self._sessions_initialized = True

    async def close_sessions(self) -> None:
        for session in self._sessions.values():
            await session.close()
        self._sessions.clear()
        self._sessions_initialized = False

    async def list_tools_detailed(self) -> dict[str, Any]:
        await self.initialize_sessions()
        detailed: dict[str, Any] = {}
        for name, session in self._sessions.items():
            detailed[name] = await session.get_tools_detailed()
        return detailed

    async def call_tool(self, tool_name: str, params: dict[str, Any] | None = None, server_name: str | None = None) -> str:
        await self.initialize_sessions()
        params = params or {}
        if server_name is None:
            for session_name, session in self._sessions.items():
                if tool_name in await session.list_tools():
                    server_name = session_name
                    break
        if not server_name:
            return json.dumps(
                {
                    "success": False,
                    "error_type": "MISSING_TOOL",
                    "error": f"Tool {tool_name} was not found in any configured MCP server.",
                },
                ensure_ascii=False,
            )
        session = self._sessions.get(server_name)
        if session is None:
            return json.dumps(
                {
                    "success": False,
                    "error_type": "MISSING_SERVER",
                    "error": f"MCP server {server_name} is not configured.",
                },
                ensure_ascii=False,
            )
        return await session.call_tool(tool_name, params)

    def build_tools_documentation(self, *, cache_ttl_seconds: float = 3.0) -> str:
        with self._cache_lock:
            now = time.time()
            if self._docs_cache and now - self._docs_cache_at <= cache_ttl_seconds:
                return self._docs_cache
            detailed = self.run(self.list_tools_detailed(), timeout=30.0)
            parts = ["Available MCP tools:"]
            for server_name, tools in detailed.items():
                if not isinstance(tools, list):
                    continue
                for tool in tools:
                    name = str(tool.get("name") or "")
                    description = str(tool.get("description") or "")
                    schema = tool.get("inputSchema") or {}
                    parts.append(f"- {name}: {description}")
                    properties = schema.get("properties") or {}
                    required = set(schema.get("required") or [])
                    for param_name, param_info in properties.items():
                        param_type = param_info.get("type", "any")
                        req = "required" if param_name in required else "optional"
                        desc = str(param_info.get("description") or "")
                        parts.append(f"  - {param_name} ({param_type}, {req}): {desc}")
            self._docs_cache = "\n".join(parts)
            self._docs_cache_at = now
            return self._docs_cache

    def inventory(self) -> list[dict[str, Any]]:
        detailed = self.run(self.list_tools_detailed(), timeout=30.0)
        inventory: list[dict[str, Any]] = []
        for server_name, tools in detailed.items():
            if not isinstance(tools, list):
                continue
            for tool in tools:
                inventory.append(
                    {
                        "server_name": server_name,
                        "name": tool.get("name", ""),
                        "summary": tool.get("description", ""),
                        "inputSchema": tool.get("inputSchema") or {},
                    }
                )
        return inventory


_runtime = _Runtime()
_local_runtime = _LocalRuntime()


def call_mcp_tool_sync(tool: str, params: dict[str, Any] | None = None, server_name: str | None = None, *, timeout: float = 300.0) -> str:
    local_sessions = _local_only_sessions()
    if local_sessions:
        async def _call_local() -> str:
            if server_name:
                session = local_sessions.get(server_name)
                if session is None:
                    return json.dumps(
                        {
                            "success": False,
                            "error_type": "MISSING_SERVER",
                            "error": f"MCP server {server_name} is not configured.",
                        },
                        ensure_ascii=False,
                    )
                return await session.call_tool(tool, params or {})
            for session in local_sessions.values():
                if tool in await session.list_tools():
                    return await session.call_tool(tool, params or {})
            return json.dumps(
                {
                    "success": False,
                    "error_type": "MISSING_TOOL",
                    "error": f"Tool {tool} was not found in any configured MCP server.",
                },
                ensure_ascii=False,
            )

        return _local_runtime.run(_call_local(), timeout=timeout)
    return _runtime.run(_runtime.call_tool(tool, params, server_name), timeout=timeout)


def get_all_tools_detailed_sync() -> dict[str, Any]:
    return _runtime.run(_runtime.list_tools_detailed(), timeout=30.0)


def close_sync_sessions() -> None:
    _runtime.run(_runtime.close_sessions(), timeout=10.0)


def build_tools_documentation_sync() -> str:
    local_sessions = _local_only_sessions()
    if local_sessions:
        async def _build_local_docs() -> str:
            parts = ["Available MCP tools:"]
            for _, session in local_sessions.items():
                tools = await session.get_tools_detailed()
                for tool in tools:
                    name = str(tool.get("name") or "")
                    description = str(tool.get("description") or "")
                    schema = tool.get("inputSchema") or {}
                    parts.append(f"- {name}: {description}")
                    properties = schema.get("properties") or {}
                    required = set(schema.get("required") or [])
                    for param_name, param_info in properties.items():
                        param_type = param_info.get("type", "any")
                        req = "required" if param_name in required else "optional"
                        desc = str(param_info.get("description") or "")
                        parts.append(f"  - {param_name} ({param_type}, {req}): {desc}")
            return "\n".join(parts)

        return _local_runtime.run(_build_local_docs(), timeout=30.0)
    return _runtime.build_tools_documentation()


def tool_inventory() -> list[dict[str, Any]]:
    local_sessions = _local_only_sessions()
    if local_sessions:
        async def _inventory_local() -> list[dict[str, Any]]:
            inventory: list[dict[str, Any]] = []
            for server_name, session in local_sessions.items():
                tools = await session.get_tools_detailed()
                for tool in tools:
                    inventory.append(
                        {
                            "server_name": server_name,
                            "name": tool.get("name", ""),
                            "summary": tool.get("description", ""),
                            "inputSchema": tool.get("inputSchema") or {},
                        }
                    )
            return inventory

        return _local_runtime.run(_inventory_local(), timeout=30.0)
    return _runtime.inventory()


def tool_exists(name: str) -> bool:
    return any(item.get("name") == name for item in tool_inventory())
