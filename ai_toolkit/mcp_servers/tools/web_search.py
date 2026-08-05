from tavily import TavilyClient
import os

client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])


def web_search(query: str) -> dict:
    response = client.search(
        query=query,
        topic="general",
        search_depth="advanced",
        include_answer=True,
        max_results=5,
    )

    return {
        "answer": response.get("answer"),
        "results": [
            {
                "title": r["title"],
                "url": r["url"],
                "content": r["content"],
            }
            for r in response.get("results", [])
        ],
    }