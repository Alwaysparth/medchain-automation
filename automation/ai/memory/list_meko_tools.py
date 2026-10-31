"""
Connect to Meko's MCP endpoint and print the tools (and resources) it offers, with their input
schemas. Read-only: it never calls a tool. Output contains NO secrets, so it is safe to share.

Setup (PowerShell, from the repo root):
    pip install "mcp>=1.9"
    $env:MEKO_MCP_URL = "https://...your Meko MCP URL..."
    $env:MEKO_API_KEY = "...your key..."          # never paste this into chat
    python automation/ai/memory/list_meko_tools.py > meko_tools.txt

Optional:
    $env:MEKO_AUTH_HEADER = "Authorization"       # header name Meko expects (default Authorization)
    $env:MEKO_AUTH_SCHEME = "Bearer"              # prefix before the key; set to "" for none
    $env:MEKO_TRANSPORT  = "http" | "sse"         # default: try http, then sse
"""
from __future__ import annotations

import asyncio
import json
import os
import sys


def headers() -> dict:
    key = os.getenv("MEKO_API_KEY", "")
    if not key:
        return {}
    scheme = os.getenv("MEKO_AUTH_SCHEME", "Bearer")
    return {os.getenv("MEKO_AUTH_HEADER", "Authorization"): f"{scheme} {key}".strip()}


async def describe(session) -> None:
    init = await session.initialize()
    info = getattr(init, "serverInfo", None)
    print(f"SERVER: {getattr(info, 'name', '?')} {getattr(info, 'version', '')}")
    if getattr(init, "instructions", None):
        print(f"SERVER INSTRUCTIONS:\n{init.instructions}\n")

    tools = (await session.list_tools()).tools
    print(f"TOOLS ({len(tools)}):")
    for t in tools:
        print("\n" + "=" * 70)
        print(f"name: {t.name}")
        print(f"description: {(t.description or '').strip()}")
        print("input schema:")
        print(json.dumps(t.inputSchema, indent=2))

    try:
        res = (await session.list_resources()).resources
        print(f"\nRESOURCES ({len(res)}):")
        for r in res:
            print(f"- {r.uri}  {r.name}  {(r.description or '')[:120]}")
    except Exception as exc:
        print(f"\nRESOURCES: not available ({type(exc).__name__})")


async def via_http(url: str) -> None:
    from mcp import ClientSession
    from mcp.client import streamable_http as sh
    client = getattr(sh, "streamablehttp_client", None) or getattr(sh, "streamable_http_client")
    async with client(url, headers=headers()) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await describe(session)


async def via_sse(url: str) -> None:
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    async with sse_client(url, headers=headers()) as (read, write):
        async with ClientSession(read, write) as session:
            await describe(session)


async def main() -> int:
    url = os.getenv("MEKO_MCP_URL")
    if not url:
        print("Set MEKO_MCP_URL (and MEKO_API_KEY) first.", file=sys.stderr)
        return 2
    transport = os.getenv("MEKO_TRANSPORT", "").lower()
    attempts = [("http", via_http), ("sse", via_sse)]
    if transport in ("http", "sse"):
        attempts = [a for a in attempts if a[0] == transport]
    for name, fn in attempts:
        try:
            print(f"(transport: {name})")
            await fn(url)
            return 0
        except Exception as exc:
            # Show the error type and message, never the headers (they contain the key)
            print(f"(transport {name} failed: {type(exc).__name__}: {str(exc)[:300]})", file=sys.stderr)
    return 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(asyncio.run(main()))
