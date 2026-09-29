import asyncio
import json
import os
import sys

from openai import OpenAI
from mcp import Client, StdioServerParameters

SERVER = os.environ.get("MCP_SERVER") or StdioServerParameters(
    command=sys.executable,
    args=[os.path.join(os.path.dirname(__file__), "tools_server.py")]
)

MODEL = os.environ.get("AGENT_MODEL", "minimax/minimax-m3:free")
BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")


def to_openai(tool):
    """Convert MCP Tool definition to OpenAI function format."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


async def run(goal: str, max_steps: int = 8):
    async with Client(SERVER) as mcp:
        tools_list = (await mcp.list_tools()).tools
        openai_tools = [to_openai(t) for t in tools_list]
        print(f"[host] {len(tools_list)} tools: {[t.name for t in tools_list]}")

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            print("Note: OPENAI_API_KEY environment variable is not set. Skipped LLM execution.", file=sys.stderr)
            return "skipped: missing OPENAI_API_KEY"

        client = OpenAI(base_url=BASE_URL, api_key=api_key)
        messages = [{"role": "user", "content": goal}]

        for step in range(max_steps):
            extra_body = {}
            if "openrouter.ai" in BASE_URL:
                extra_body = {"reasoning": {"enabled": False}}

            resp = client.chat.completions.create(
                model=MODEL,
                tools=openai_tools,
                messages=messages,
                extra_body=extra_body if extra_body else None,
            )
            msg = resp.choices[0].message
            messages.append(msg)

            if not msg.tool_calls:
                return msg.content or ""

            for call in msg.tool_calls:
                args = json.loads(call.function.arguments) if call.function.arguments else {}
                result = await mcp.call_tool(call.function.name, args)
                out = "\n".join(c.text for c in result.content if c.type == "text")
                print(f"  [tool] {call.function.name}({args}) -> {out}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": str(out),
                })

        return "stopped: max steps exceeded"


if __name__ == "__main__":
    goal = sys.argv[1] if len(sys.argv) > 1 else "Read notes.txt and sum the numbers in it."
    result = asyncio.run(run(goal))
    print(result)
