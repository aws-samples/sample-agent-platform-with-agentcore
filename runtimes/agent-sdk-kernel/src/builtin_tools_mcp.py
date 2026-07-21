#!/usr/bin/env python3
"""Stdio MCP server wrapping the AgentCore built-in tools.

Usage: python3 builtin_tools_mcp.py <code-interpreter|browser>

One process wraps exactly one built-in tool, so the platform registry can
attach them independently. Both kernels install this file at
/opt/platform/builtin_tools_mcp.py and reference it from their MCP config
(``.mcp.json`` for the interactive kernel, ``ClaudeAgentOptions.mcp_servers``
for the headless kernel) — a registry entry with kind ``builtin`` resolves to
this script with the tool name as the argument.

Sessions are lazy: the first tool call starts an AgentCore session (billed per
session-second) and it is reused for every later call in this MCP server's
lifetime, so sandbox state / browser state carries across calls.

NOTE: keep in sync with the copy in runtimes/claude-code-kernel/scripts/.
"""

import atexit
import os
import sys

from mcp.server.fastmcp import FastMCP, Image

REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
TOOL = sys.argv[1] if len(sys.argv) > 1 else ""

mcp = FastMCP(f"agentcore-{TOOL}")


if TOOL == "code-interpreter":
    from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter

    _client: list = []  # lazily-started singleton

    def _session() -> CodeInterpreter:
        if not _client:
            c = CodeInterpreter(REGION)
            c.start()
            atexit.register(c.stop)
            _client.append(c)
        return _client[0]

    def _run(tool_name: str, arguments: dict) -> str:
        response = _session().invoke(tool_name, arguments)
        parts: list[str] = []
        for event in response["stream"]:
            result = event.get("result", {})
            for item in result.get("content", []):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
            if result.get("isError"):
                parts.append("[execution reported an error]")
        return "\n".join(p for p in parts if p) or "(no output)"

    @mcp.tool()
    def execute_python(code: str) -> str:
        """Run Python code in the AgentCore Code Interpreter sandbox.

        Variables and files persist across calls within this session, so you
        can build up state incrementally. stdout/stderr are returned.
        """
        return _run("executeCode", {"language": "python", "code": code, "clearContext": False})

    @mcp.tool()
    def execute_command(command: str) -> str:
        """Run a shell command in the AgentCore Code Interpreter sandbox."""
        return _run("executeCommand", {"command": command})


elif TOOL == "browser":
    from bedrock_agentcore.tools.browser_client import BrowserClient
    from playwright.async_api import async_playwright

    # Playwright's sync API is thread-affine and FastMCP runs sync tools on a
    # worker thread pool — use the async API on the event loop instead.
    _state: dict = {}

    async def _page():
        if "page" not in _state:
            client = BrowserClient(REGION)
            client.start()
            atexit.register(client.stop)
            ws_url, headers = client.generate_ws_headers()
            pw = await async_playwright().start()
            browser = await pw.chromium.connect_over_cdp(ws_url, headers=headers)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            _state["page"] = context.pages[0] if context.pages else await context.new_page()
        return _state["page"]

    @mcp.tool()
    async def navigate(url: str) -> str:
        """Open a URL in the AgentCore cloud browser. Returns the page title.

        The first call starts the browser session (takes several seconds);
        later calls reuse it, so cookies and login state persist.
        """
        page = await _page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        return f"title: {await page.title()}\nurl: {page.url}"

    @mcp.tool()
    async def get_page_text(max_chars: int = 6000) -> str:
        """Return the visible text of the current page (truncated to max_chars)."""
        page = await _page()
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        return text[:max_chars] or "(empty page)"

    @mcp.tool()
    async def click(selector: str) -> str:
        """Click the element matching a CSS selector on the current page."""
        page = await _page()
        await page.click(selector, timeout=10_000)
        return f"clicked {selector}; now at {page.url}"

    @mcp.tool()
    async def fill(selector: str, text: str) -> str:
        """Type text into the element matching a CSS selector."""
        page = await _page()
        await page.fill(selector, text, timeout=10_000)
        return f"filled {selector}"

    @mcp.tool()
    async def screenshot() -> Image:
        """Take a PNG screenshot of the current page."""
        page = await _page()
        return Image(data=await page.screenshot(type="png"), format="png")


else:
    print(f"usage: builtin_tools_mcp.py <code-interpreter|browser> (got {TOOL!r})", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    mcp.run(transport="stdio")
