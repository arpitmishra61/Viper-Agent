def web_search(query: str, max_results: int = 5) -> str:
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        if not results:
            return "No results found."
        parts = []
        for r in results:
            parts.append(
                f"Title: {r.get('title', '')}\n"
                f"Summary: {r.get('body', '')}\n"
                f"URL: {r.get('href', '')}"
            )
        return "\n\n".join(parts)
    except ImportError:
        return "ddgs not installed. Run: pip install ddgs"
    except Exception as e:
        return f"Search failed: {e}"


DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web for current information, news, facts, or anything "
                "you don't know or that may have changed since your training cutoff."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Number of results (1-10, default 5)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
]
