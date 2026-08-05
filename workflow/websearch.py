import asyncio
import yaml

from ai_toolkit.llm import generate_with_mcp


async def main():
    with open("workflow/config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    llm_cfg = config["llm"]

    prompt = "What is an LLM?"

    response = await generate_with_mcp(
        prompt=prompt,
        provider=llm_cfg["provider"],
        model=llm_cfg["model"],
        base_url=llm_cfg.get("base_url"),
        api_key=llm_cfg.get("api_key"),
        mcp_servers=llm_cfg["mcp_servers"],
    )

    return response


if __name__ == "__main__":
    asyncio.run(main())

# uv run mcp run ai_toolkit/mcp_servers/servers/websearch_server.py