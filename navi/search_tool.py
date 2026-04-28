"""DuckDuckGo text search wrapper for NAVI's `search_web` tool (ddgs package)."""

from ddgs import DDGS


def search_web(query, max_results=3):
    """Searches the web and returns summarized results."""
    try:
        results = list(DDGS().text(query, max_results=max_results))
        if not results:
            return "No results found."
        output = []
        for r in results:
            output.append(f"{r['title']}: {r['body']}")
        return "\n\n".join(output)
    except Exception as e:
        return f"Search failed: {e}"