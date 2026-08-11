"""Meta tools: tool discovery, session journal views, timeouts, and issue reporting."""

from mcp.types import TextContent, Tool

from ..issue_reporter import DEFAULT_REPO, build_issue_body, issue_fingerprint
from ..metrics import compute_metrics
from ..server_metadata import mcp_version, server_info
from ..tool_response import content_error, content_success
from .context import ToolContext
from .registry import register


@register(Tool(
    name="mcp_info",
    description="Identifies THIS running stm32-gdb-mcp server: release version, git commit, the "
                "directory the code was imported from, whether compact mode is hiding tools, and "
                "how many tools this build advertises. Needs NO debug session, NO probe and NO "
                "target — call it the moment a tool you expect is missing or behaves like an older "
                "build, instead of guessing which checkout the client actually launched.",
    inputSchema={"type": "object", "properties": {}}
))
def mcp_info(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    info = server_info()
    catalog = ctx.tool_catalog()
    if catalog:
        # Populated only once the client has listed tools; an empty catalog would
        # read as "this build advertises nothing", which is worse than silence.
        info["tools_advertised"] = len(catalog)
    return [content_success(info, suggested_next_actions=["tool_help"])]


@register(Tool(
    name="tool_help",
    description="Returns descriptions and complete schemas for full-surface tools, including tools hidden in compact mode.",
    inputSchema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Exact tool name."},
            "query": {"type": "string", "description": "Case-insensitive name/description search."},
        },
    },
))
def tool_help(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    catalog = ctx.tool_catalog()
    requested_name = arguments.get("name")
    query = (arguments.get("query") or "").strip().lower()
    if requested_name:
        matches = [catalog[requested_name]] if requested_name in catalog else []
    elif query:
        matches = [
            tool
            for tool in catalog.values()
            if query in tool.name.lower() or query in (tool.description or "").lower()
        ]
    else:
        return [content_error(
            "tool_help requires name or query.",
            code="missing_argument",
        )]
    return [content_success({
        "count": len(matches),
        # count=0 for a tool the agent is sure exists means one of two things, and
        # only the build identity separates them: wrong build, or right build with
        # the tool renamed. Answering both in one call is what stops the
        # restart-the-client-and-look-again loop.
        "server": server_info(),
        "tools": [tool.model_dump(by_alias=True, exclude_none=True) for tool in matches],
    })]


@register(Tool(
    name="get_session",
    description="Returns this session's record by view: 'journal' (every tool call with "
                "args/ok/summary/duration; supports limit), 'timeline' (compact human-readable "
                "replay), or 'metrics' (per-tool call counts, success/failure, durations).",
    inputSchema={
        "type": "object",
        "properties": {
            "view": {"type": "string", "enum": ["journal", "timeline", "metrics"], "description": "Which view (default journal)."},
            "limit": {"type": "integer", "description": "journal: return only the most recent N entries."}
        }
    }
))
def get_session(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    view = arguments.get("view", "journal")
    if view == "timeline":
        return [content_success({"run_id": ctx.session_journal.run_id, "timeline": ctx.session_journal.timeline()})]
    if view == "metrics":
        return [content_success({"run_id": ctx.session_journal.run_id, **compute_metrics(ctx.session_journal.get())})]
    if view == "journal":
        entries = ctx.session_journal.get(limit=arguments.get("limit"))
        return [content_success({"run_id": ctx.session_journal.run_id, "count": len(entries), "entries": entries})]
    raise ValueError(f"Unknown session view '{view}'. Use journal, timeline, or metrics.")


@register(Tool(
    name="clear_session_journal",
    description="Clears the session journal (keeps the run-id).",
    inputSchema={"type": "object", "properties": {}}
))
def clear_session_journal(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    ctx.session_journal.clear()
    return [content_success({"message": "Session journal cleared", "run_id": ctx.session_journal.run_id})]


@register(Tool(
    name="get_timeouts",
    description="Returns the current named GDB operation timeouts (connect, reset, memory, "
                "registers, source, run, download).",
    inputSchema={"type": "object", "properties": {}}
))
def get_timeouts(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    return [content_success({"timeouts": ctx.gdb_client.timeouts.as_dict()})]


@register(Tool(
    name="set_timeouts",
    description="Overrides one or more named timeouts (positive seconds). Useful for a slow "
                "or flaky probe. Recorded in the journal so replays are deterministic.",
    inputSchema={
        "type": "object",
        "properties": {
            "overrides": {
                "type": "object",
                "description": "Map of timeout name -> seconds, e.g. {\"memory\": 4.0, \"connect\": 8.0}."
            }
        },
        "required": ["overrides"]
    }
))
def set_timeouts(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    updated = ctx.gdb_client.timeouts.set(arguments["overrides"])
    return [content_success({"message": "Timeouts updated", "timeouts": updated})]


@register(Tool(
    name="report_issue",
    description="Files a GitHub issue about an MCP problem from THIS session — auto-bundling "
                "the session journal, metrics, and MCP version into a structured report (via "
                "the gh CLI). Call this when a tool misbehaves or confuses you. Deduplicated "
                "per session so retries don't spam. Defaults to the stm32-gdb-mcp repo.",
    inputSchema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short issue title, e.g. '[agent] start_debug_session ignores server_args'."},
            "description": {"type": "string", "description": "What you were doing and what went wrong."},
            "env": {"type": "string", "description": "Agent/IDE + model, e.g. 'Cursor / deepseek-v4-pro'."},
            "repo": {"type": "string", "description": "owner/repo to file under (default the MCP repo)."}
        },
        "required": ["title", "description"]
    }
))
def report_issue(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    title = arguments["title"]
    fp = issue_fingerprint(title)
    if fp in ctx.reported_issues:
        return [content_success({
            "message": "Already reported this session (deduplicated)",
            "url": ctx.reported_issues[fp],
        })]
    body = build_issue_body(
        description=arguments.get("description"),
        env=arguments.get("env"),
        version=mcp_version(),
        journal=ctx.session_journal.get(limit=25),
        metrics=compute_metrics(ctx.session_journal.get()),
    )
    repo = arguments.get("repo") or DEFAULT_REPO
    result = ctx.fns.file_issue(repo, title, body)
    if result["ok"]:
        ctx.reported_issues[fp] = result["url"]
        return [content_success({"message": "Issue filed", "url": result["url"], "repo": repo})]
    return [content_error(
        f"Could not file the issue automatically: {result['error']}",
        code="issue_filing_failed",
        raw_response={"repo": repo, "title": title, "prepared_body": result.get("body")},
        suggested_next_actions=["get_session"],
    )]
