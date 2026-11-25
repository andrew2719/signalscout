"""Call every SignalScout MCP tool through a real MCP client (Streamable HTTP).

  conda run -n ml python check_mcp.py                                   # local server on port 8000
  conda run -n ml python check_mcp.py https://<your-service>.onrender.com/mcp

On Render's free tier, open https://<your-service>.onrender.com/health first: a sleeping
service needs about a minute to wake up.
"""
import asyncio
import sys

from mcp import Client

CALLS = [
    ("get_data_coverage", {}),
    ("search_articles", {"query": "AI", "limit": 3}),
    ("search_articles", {"query": "expansion", "limit": 3}),
    ("get_organization_news", {"organization": "Cleveland Clinic", "limit": 3}),
    ("get_recent_articles", {"days": 30, "limit": 3}),
]


async def main(url: str) -> int:
    failures = 0
    async with Client(url) as client:
        tools = (await client.list_tools()).tools
        print(f"Connected to {url}\nTools: {', '.join(tool.name for tool in tools)}")
        for name, arguments in CALLS:
            result = await client.call_tool(name, arguments)
            print(f"\n{name}({arguments})")
            if result.is_error:
                failures += 1
                print(f"  ERROR: {result.content[0].text}")
                continue
            data = result.structured_content
            if name == "get_data_coverage":
                print(f"  {data['total_articles']} articles from {data['organization_count']} organizations, "
                      f"published {data['earliest_published_date']} to {data['latest_published_date']}, "
                      f"last fetch {data['latest_successful_fetch']}")
                continue
            print(f"  {data['result_count']} article(s)" +
                  (f", topics {data['matched_topics']}" if "matched_topics" in data else ""))
            for article in data["articles"]:
                how = f" [{'+'.join(article['matched_by'])}]" if "matched_by" in article else ""
                print(f"  - {article['published_date'] or 'no date'} | {article['organization']} | {article['title'][:70]}{how}")
                print(f"    {article['url']}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000/mcp")))
