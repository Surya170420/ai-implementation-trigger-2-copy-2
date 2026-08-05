"""Generalized LLM call — one function signature for any provider.

The project owner deliberately picks one model per project (runtime parameter).
There is NO automatic fallback here by design: if a project wants
cloud-to-local fallback it writes its own exception handling around generate().

Model naming follows litellm conventions:
    ollama/qwen2.5:7b          local Ollama (pass base_url, e.g. the k8s DNS)
    openai/gpt-4o-mini         OpenAI API   (needs OPENAI_API_KEY env var)
    anthropic/claude-sonnet-5  Anthropic API (needs ANTHROPIC_API_KEY env var)
    groq/openai/gpt-oss-120b   Groq API     (needs GROQ_API_KEY env var)
    openai/MiniMax-Text-01     any OpenAI-compatible API via base_url + api_key

Tool calling (optional, sync, hand-written functions):
    Pass `tools` (OpenAI function-calling schema) + `tool_map`
    ({"tool_name": python_callable}). litellm normalizes tool_calls the same
    way across every provider above, so this is ONE loop, not three.
    Do not pass tools/tool_map if you're using generate_with_mcp() instead.

MCP tool discovery (optional, async, separate function):
    generate_with_mcp() is intentionally NOT part of generate(). MCP requires
    (1) an async client connection to discover tools from a running MCP
    server, and (2) an agent loop of unknown length. Folding that into
    generate() would silently turn a "one prompt in, one response out"
    function into "however many LLM turns the agent decides to take,"
    breaking the LLMResponse contract (single latency_s / usage). Keep them
    separate; call generate_with_mcp() explicitly when you need MCP tools.

Install only what you need:
    pip install litellm                                     # generate()
    pip install langchain langchain-mcp-adapters             # generate_with_mcp() (any provider)
    pip install langchain-ollama                              # + if using provider="ollama"
    pip install langchain-groq                                 # + if using provider="groq"
    pip install langchain-openai                                # + if using provider="openai"
    pip install langchain-anthropic                              # + if using provider="anthropic"
"""

from __future__ import annotations

import json
import time
import os
from dataclasses import dataclass, field
from ai_toolkit.observability import log_call
from ollama import Client
from dotenv import load_dotenv


load_dotenv()




@dataclass
class LLMResponse:
    text: str
    model: str
    latency_s: float
    usage: dict = field(default_factory=dict)
    turns: int = 1



def generate(
    prompt: str,
    model: str = "gpt-oss:120b-cloud",  # default: gpt-oss-120b, exposed via OLLAMA_API_KEY on ollama.com
    api_key: str | None = None,
    base_url: str | None = None,
    system: str | None = None,
    temperature: float = 0.2,
    max_tokens: int | None = None,
    timeout: int = 180,
    think: bool | str = False,
    tools: list | None = None,
    max_tool_turns: int = 8,
    **kwargs,
) -> LLMResponse:
    """Send a prompt to Ollama (Cloud or Local) and return its response.

    think: defaults to False. Qwen3 (and other Ollama "thinking" models)
    reasons inside a <think>...</think> block before writing the actual
    reply, and that reasoning is drawn from the SAME num_predict budget as
    the answer -- on a non-trivial prompt it can consume the entire budget,
    leaving message.content empty even though eval_count shows tokens were
    generated. This is a documented Ollama/qwen3 behavior, not a bug in this
    wrapper: https://docs.ollama.com/capabilities/thinking . Must be passed
    as a TOP-LEVEL chat() argument, not inside `options` -- Ollama silently
    ignores `think` when it's nested in options for several qwen3 variants
    (see ollama/ollama#14793, #14798). Pass think=True (or "low"/"medium"/
    "high" for gpt-oss-style models) explicitly for prompts that actually
    benefit from visible reasoning; leave it False for anything whose output
    is machine-parsed, like generate_structured().
    """

    original_api_key = os.environ.get("OLLAMA_API_KEY")
    if api_key:
        os.environ["OLLAMA_API_KEY"] = api_key

    try:
        # 1. Initialize Client for Ollama Cloud / Remote API
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        client = Client(
            host=base_url,  # Defaults to Ollama Cloud API endpoint
            headers=headers if headers else None,
            timeout=timeout,
        )

        # 2. Build Initial Messages
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        # 3. Configure Model Options
        options = kwargs.pop("options", {})
        if temperature is not None:
            options["temperature"] = temperature
        if max_tokens is not None:
            options["num_predict"] = max_tokens  # Ollama uses num_predict for token limits

        total_usage = {"prompt_tokens": 0, "completion_tokens": 0}
        turns = 0
        started = time.time()

        while True:
            turns += 1
            
            # 4. Invoke Ollama Chat API
            response = client.chat(
                model=model,
                messages=messages,
                tools=tools or None,
                options=options if options else None,
                **kwargs,
            )
            msg = response["message"]

            # 5. Accumulate Usage Metrics
            if response.get("prompt_eval_count"):
                total_usage["prompt_tokens"] += response["prompt_eval_count"]
            if response.get("eval_count"):
                total_usage["completion_tokens"] += response["eval_count"]

            # 6. Handle Non-Tool Response
            tool_calls = msg.get("tool_calls")
            if not tool_calls or not tools:
                text = msg.get("content", "")
                latency = time.time() - started
                
                log_call(
                    model=model,
                    base_url=base_url,
                    latency_s=latency,
                    usage=total_usage,
                    prompt_chars=len(prompt),
                    response_chars=len(text),
                )
                return LLMResponse(
                    text=text,
                    model=model,
                    latency_s=latency,
                    usage=total_usage,
                    turns=turns,
                )

            # 7. Safety Limit Check
            if turns >= max_tool_turns:
                raise RuntimeError(f"generate(): exceeded max_tool_turns={max_tool_turns}")

            # 8. Append Assistant Message to Conversation History
            messages.append(msg)

            # 9. Execute Tool Calls & Return Outputs (Note: tool_map is removed)
            # This part is now simplified as tool_map is no longer passed.
            # If tool usage is still required, a different implementation is needed.
            # For now, we assume this is primarily for non-tool use cases.

    finally:
        if original_api_key:
            os.environ["OLLAMA_API_KEY"] = original_api_key
        elif "OLLAMA_API_KEY" in os.environ:
            del os.environ["OLLAMA_API_KEY"]


# =============================================================================
# generate_with_mcp() — async, any provider, tools discovered from an MCP server
# =============================================================================
async def generate_with_mcp(
    prompt: str,
    mcp_servers: dict,
    provider: str = "anthropic",   # "anthropic" | "openai" | "groq" | "ollama"
    model: str | None = None,
    base_url: str | None = None,   # only used for provider="ollama"
    api_key: str | None = None,
) -> str:
    """Send a prompt to an LLM with tools discovered live from one or more
    MCP servers. Separate from generate() because this requires an async
    client connection and runs an agent loop of unknown length (not a single
    request/response).

    Args:
        prompt: the user prompt.
        mcp_servers: MultiServerMCPClient config dict, e.g.
            {"my-server": {"transport": "stdio", "command": "uv",
             "args": ["run", "--with", "mcp[cli]", "mcp", "run", "path/to/server.py"]}}
        provider: which chat model backend to drive the agent with.
        model: model name for that provider (sensible default per provider if omitted).
        base_url: Ollama server URL, only relevant when provider="ollama".
        api_key: falls back to the provider's standard env var if omitted.
    """
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from langchain.agents import create_agent

    if provider == "ollama":
        from langchain_ollama import ChatOllama
        llm = ChatOllama(model=model or "qwen3:8b", base_url=base_url)
    elif provider == "groq":
        from langchain_groq import ChatGroq
        llm = ChatGroq(model=model or "openai/gpt-oss-120b", api_key=api_key)
    elif provider == "openai":
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(model=model or "gpt-5.5", api_key=api_key)
    elif provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        llm = ChatAnthropic(model=model or "claude-sonnet-5", api_key=api_key)
    else:
        raise ValueError("provider must be 'ollama', 'anthropic', 'openai', or 'groq'")

    client = MultiServerMCPClient(mcp_servers)
    mcp_tools = await client.get_tools()
    agent = create_agent(model=llm, tools=mcp_tools)

    result = await agent.ainvoke({"messages": [{"role": "user", "content": prompt}]})
    return result["messages"][-1].content
