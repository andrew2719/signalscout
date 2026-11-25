"""Tests for the MCP server (server.py) and its read-only database helpers.

The default tests run offline: database functions are replaced by small fakes and the tools are
called through a real in-memory MCP client. The last two tests are opt-in and use MongoDB Atlas:
      SIGNALSCOUT_DB_TESTS=1 conda run -n ml python -m pytest -v tests/test_server.py
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import anyio
import pytest
from bson import ObjectId
from dotenv import load_dotenv
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError
from pymongo import MongoClient, monitoring
from pymongo.errors import ServerSelectionTimeoutError
from starlette.testclient import TestClient

import database as db
import server

PROJECT_DIR = Path(__file__).resolve().parents[1]
DB_TESTS = pytest.mark.skipif(os.getenv("SIGNALSCOUT_DB_TESTS") != "1", reason="set SIGNALSCOUT_DB_TESTS=1 to use MongoDB Atlas")


def make_doc(slug: str, tags: list[str], date: str | None = "2026-11-20", source: str = "cleveland_clinic") -> dict:
    names = {"cleveland_clinic": "Cleveland Clinic", "vanderbilt_health": "Vanderbilt University Medical Center",
             "intermountain_health": "Intermountain Health"}
    return {"title": f"Story {slug}", "source": {"id": source, "name": names[source]}, "published_date": date,
            "description": f"About {slug}", "content": f"Excerpt of {slug}", "content_scope": "excerpt",
            "tags": tags, "canonical_url": f"https://news.example.org/{slug}"}


SOURCES = [  # what database.source_stats returns
    {"_id": "cleveland_clinic", "organization": "Cleveland Clinic", "article_count": 10,
     "earliest_published_date": "2026-10-01", "latest_published_date": "2026-11-23",
     "first_collected_at": datetime(2026, 11, 25, 1, 54), "latest_fetch_at": datetime(2026, 11, 25, 2, 29)},
    {"_id": "vanderbilt_health", "organization": "Vanderbilt University Medical Center", "article_count": 20,
     "earliest_published_date": "2026-11-10", "latest_published_date": "2026-11-24",
     "first_collected_at": datetime(2026, 11, 25, 1, 54), "latest_fetch_at": datetime(2026, 11, 25, 2, 27)},
    {"_id": "intermountain_health", "organization": "Intermountain Health", "article_count": 20,
     "earliest_published_date": None, "latest_published_date": None,
     "first_collected_at": datetime(2026, 11, 25, 1, 55), "latest_fetch_at": datetime(2026, 11, 25, 2, 28)},
]


@pytest.fixture
def fake_db(monkeypatch):
    """Replace every read function the server uses with a fake; record the text-search queries."""
    calls = {"text_queries": []}

    def fake_search(collection, query, limit):
        calls["text_queries"].append(query)
        return [{**make_doc("expands-clinic", ["expansion"]), "score": 2.5}] if "expand" in query else []

    monkeypatch.setattr(server, "_collection", lambda: "fake-collection")
    monkeypatch.setattr(db, "search_articles", fake_search)
    monkeypatch.setattr(db, "find_by_tags", lambda c, tags, limit: [make_doc(f"tag-{t}", [t]) for t in tags])
    monkeypatch.setattr(db, "source_stats", lambda c: SOURCES)
    monkeypatch.setattr(db, "find_by_source", lambda c, source_id, limit: [make_doc("news", [], source=source_id)])
    monkeypatch.setattr(db, "find_published_since", lambda c, since, limit: [make_doc("recent", [], "2026-11-24")])
    monkeypatch.setattr(db, "count_without_date", lambda c: 3)
    return calls


def call_tool(name: str, arguments: dict):
    """Call one tool through a real MCP client connected in-process to the server."""
    async def run():
        async with Client(server.mcp) as client:
            return await client.call_tool(name, arguments)
    return anyio.run(run)


# ---------- Query handling ----------

def test_clean_query_keeps_words_only():
    assert server.clean_query('"heart failure" -cancer AI!  $where') == "heart failure cancer AI where"
    assert server.clean_query("  !!!  ") == ""


@pytest.mark.parametrize("query, topics", [
    ("AI", ["ai"]),
    ("artificial intelligence tools", ["ai"]),
    ("Expanded hospital partnerships", ["partnership", "expansion"]),
    ("acquired a practice", ["acquisition"]),
    ("financial aid for students", []),   # "aid" is not "ai" + d
    ("raising awareness", []),
])
def test_find_topics_understands_synonyms(query, topics):
    assert server.find_topics(query) == topics


def test_merge_results_removes_duplicates_and_says_why():
    a, b, c = make_doc("a", []), make_doc("b", ["ai"]), make_doc("c", ["ai", "partnership"])
    merged = server.merge_results([{**a, "score": 3.0}, {**b, "score": 1.0}], [b, c], ["ai", "partnership"], limit=10)
    assert [m["url"].rsplit("/", 1)[1] for m in merged] == ["a", "b", "c"]  # text order first, then tag-only
    assert [m["matched_by"] for m in merged] == [["text"], ["text", "tags"], ["tags"]]
    assert all("score" not in m for m in merged)  # no relevance scores are passed on
    assert len(server.merge_results([a], [b, c], ["ai"], limit=2)) == 2


def test_article_result_is_json_friendly_and_has_its_url():
    stored = {**make_doc("x", ["ai"]), "_id": ObjectId(), "content_hash": "f" * 64,
              "first_seen_at": datetime(2026, 11, 25), "metadata": {"word_count": 500}}
    result = server.article_result(stored)
    assert result["url"] == "https://news.example.org/x"
    assert not {"_id", "content_hash", "first_seen_at", "metadata"} & set(result)
    json.dumps(result)  # would raise for ObjectId / datetime


@pytest.mark.parametrize("name, source_id", [
    ("Cleveland Clinic", "cleveland_clinic"), ("CLEVELAND_CLINIC", "cleveland_clinic"),
    ("vanderbilt", "vanderbilt_health"), ("VUMC", "vanderbilt_health"), ("intermountain-health", "intermountain_health"),
])
def test_find_organization_accepts_names_ids_and_initials(name, source_id):
    assert server.find_organization(name, SOURCES)["_id"] == source_id


def test_find_organization_rejects_unknown_and_ambiguous_names():
    with pytest.raises(ToolError, match="Unknown organization.*Available organizations: Cleveland Clinic"):
        server.find_organization("Mayo Clinic", SOURCES)
    with pytest.raises(ToolError, match="matches several organizations"):
        server.find_organization("health", SOURCES)


# ---------- MCP tools through an in-memory MCP client ----------

def test_four_read_only_tools_are_listed():
    async def run():
        async with Client(server.mcp) as client:
            return (await client.list_tools()).tools
    tools = {tool.name: tool for tool in anyio.run(run)}
    assert set(tools) == {"search_articles", "get_organization_news", "get_recent_articles", "get_data_coverage"}
    assert all(tool.annotations.read_only_hint for tool in tools.values())
    assert tools["search_articles"].input_schema["properties"]["limit"]["maximum"] == 20


def test_search_uses_synonyms_and_tags(fake_db):
    result = call_tool("search_articles", {"query": "expansion", "limit": 5})
    assert not result.is_error
    data = result.structured_content
    assert fake_db["text_queries"] == ["expansion expand"]  # the synonym was added to the text search
    assert data["matched_topics"] == ["expansion"]
    assert [a["matched_by"] for a in data["articles"]] == [["text"], ["tags"]]
    assert all(a["url"].startswith("https://") for a in data["articles"])
    assert json.loads(result.content[0].text) == data  # the text copy is the same JSON


def test_search_finds_ai_articles_through_tags_when_text_search_finds_none(fake_db):
    data = call_tool("search_articles", {"query": "AI"}).structured_content
    assert data["result_count"] == 1 and data["articles"][0]["matched_by"] == ["tags"]


def test_search_with_no_results_returns_an_empty_list_and_a_hint(fake_db):
    data = call_tool("search_articles", {"query": "zebra"}).structured_content
    assert data["articles"] == [] and data["result_count"] == 0 and "get_data_coverage" in data["message"]


@pytest.mark.parametrize("name, arguments", [
    ("search_articles", {"query": "   "}),
    ("search_articles", {"query": "x" * 201}),
    ("search_articles", {"query": "AI", "limit": 21}),
    ("search_articles", {"query": "AI", "limit": 0}),
    ("get_recent_articles", {"days": 0}),
    ("get_recent_articles", {"days": 1000}),
    ("get_organization_news", {"organization": "Mayo Clinic"}),
])
def test_invalid_arguments_return_a_tool_error(fake_db, name, arguments):
    result = call_tool(name, arguments)
    assert result.is_error


def test_organization_news_and_recent_articles(fake_db):
    news = call_tool("get_organization_news", {"organization": "vumc", "limit": 3}).structured_content
    assert news["organization"] == "Vanderbilt University Medical Center" and news["articles_in_database"] == 20
    recent = call_tool("get_recent_articles", {"days": 7}).structured_content
    assert recent["days"] == 7 and recent["articles"][0]["published_date"] == "2026-11-24"


def test_data_coverage_summarises_the_database(fake_db):
    data = call_tool("get_data_coverage", {}).structured_content
    assert data["total_articles"] == 50 and data["organization_count"] == 3
    assert data["earliest_published_date"] == "2026-10-01" and data["latest_published_date"] == "2026-11-24"
    assert data["latest_successful_fetch"] == "2026-11-25T02:29:00+00:00"
    assert data["articles_without_publication_date"] == 3 and "not a complete history" in data["limitations"]


def test_database_errors_become_a_generic_message(monkeypatch):
    def broken(collection, *args):
        raise ServerSelectionTimeoutError("ac-abc123-shard-00-00.cluster.mongodb.net:27017: timed out")
    monkeypatch.setattr(server, "_collection", lambda: "fake-collection")
    monkeypatch.setattr(db, "source_stats", broken)
    result = call_tool("get_data_coverage", {})
    assert result.is_error and "temporarily unavailable" in result.content[0].text
    assert "mongodb.net" not in result.content[0].text  # internal connection details are not exposed


def test_tools_only_call_read_methods(monkeypatch):
    collection = MagicMock()  # records every method the tools call on the collection
    collection.aggregate.return_value = SOURCES
    collection.count_documents.return_value = 0
    monkeypatch.setattr(server, "_collection", lambda: collection)
    for name, arguments in [("search_articles", {"query": "AI partnership"}), ("get_recent_articles", {}),
                            ("get_organization_news", {"organization": "Cleveland Clinic"}), ("get_data_coverage", {})]:
        assert not call_tool(name, arguments).is_error
    called = {method for method, _, _ in collection.method_calls}
    assert called <= {"find", "aggregate", "count_documents"}, called


# ---------- HTTP: health endpoint and host validation ----------

def http_app():
    return server.mcp.streamable_http_app(host="0.0.0.0", transport_security=server.transport_security(), **server.HTTP_OPTIONS)


INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}}
MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def test_health_endpoint_works_for_any_host():
    with TestClient(http_app()) as client:
        response = client.get("/health", headers={"Host": "10.0.0.5:10000"})
    assert response.status_code == 200 and response.json() == {"status": "ok"}


def test_mcp_endpoint_accepts_the_render_hostname_and_rejects_others(monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_HOSTNAME", "signalscout.onrender.com")
    with TestClient(http_app()) as client:
        ok = client.post("/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Host": "signalscout.onrender.com"})
        local = client.post("/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Host": "localhost:8000"})
        evil = client.post("/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Host": "evil.example.com"})
    assert ok.status_code == 200 and local.status_code == 200
    assert evil.status_code == 421


# ---------- Opt-in: real server process, real MCP client, real MongoDB ----------

def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def collection_snapshot(collection) -> tuple:
    docs = sorted((d["canonical_url"], d["content_hash"], str(d["last_fetched_at"]))
                  for d in collection.find({}, {"canonical_url": 1, "content_hash": 1, "last_fetched_at": 1}))
    return len(docs), docs, sorted(collection.index_information())


@DB_TESTS
def test_real_server_over_http_with_real_data():
    collection = db.get_collection()
    before = collection_snapshot(collection)
    port = free_port()
    process = subprocess.Popen([sys.executable, "server.py"], cwd=PROJECT_DIR, env={**os.environ, "PORT": str(port)},
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):  # wait until the HTTP server answers /health
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.5)

        async def run():
            async with Client(f"http://127.0.0.1:{port}/mcp") as client:
                tools = [tool.name for tool in (await client.list_tools()).tools]
                results = {name: await client.call_tool(name, args) for name, args in [
                    ("get_data_coverage", {}), ("search_articles", {"query": "AI"}),
                    ("get_organization_news", {"organization": "Mass General Brigham", "limit": 5}),
                    ("get_recent_articles", {"days": 30, "limit": 5})]}
                results["empty"] = await client.call_tool("search_articles", {"query": "zzqxjv"})
                return tools, results
        tools, results = anyio.run(run)
    finally:
        process.terminate()
        process.wait(timeout=10)

    assert len(tools) == 4
    assert not any(r.is_error for r in results.values())
    coverage = results["get_data_coverage"].structured_content
    assert coverage["total_articles"] == before[0] and coverage["organization_count"] >= 5
    for name in ("search_articles", "get_organization_news", "get_recent_articles"):
        data = results[name].structured_content
        json.dumps(data)
        assert data["articles"] and all(a["url"].startswith("https://") for a in data["articles"])
    # Tags improve retrieval: plain MongoDB text search finds fewer AI articles than the tool.
    plain = db.search_articles(collection, "AI", 20)
    ai = results["search_articles"].structured_content
    assert len(ai["articles"]) > len(plain) and any("tags" in a["matched_by"] for a in ai["articles"])
    assert results["empty"].structured_content["result_count"] == 0
    assert collection_snapshot(collection) == before  # nothing was written, no index was created


class CommandRecorder(monitoring.CommandListener):
    def __init__(self):
        self.commands = []

    def started(self, event):
        self.commands.append(event.command_name)

    def succeeded(self, event):
        pass

    def failed(self, event):
        pass


@DB_TESTS
def test_real_tools_send_only_read_commands(monkeypatch):
    """Record every command the tools send to MongoDB and check that none of them writes."""
    recorder = CommandRecorder()
    load_dotenv(db.ENV_FILE)  # same configuration as database.get_client()
    monitored = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=10_000, event_listeners=[recorder])
    monkeypatch.setattr(server, "_collection", lambda: monitored[db.DB_NAME][db.COLLECTION_NAME])
    for name, arguments in [("search_articles", {"query": "expansion"}), ("get_recent_articles", {}),
                            ("get_organization_news", {"organization": "Cleveland Clinic"}), ("get_data_coverage", {})]:
        assert not call_tool(name, arguments).is_error
    monitored.close()
    writes = {"insert", "update", "delete", "findAndModify", "createIndexes", "drop", "dropIndexes", "create"}
    assert recorder.commands and not writes & set(recorder.commands), recorder.commands
