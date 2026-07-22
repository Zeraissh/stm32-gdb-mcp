"""FreeRTOS runtime inspection tools (detection, tasks, queues, heap, snapshot)."""

from mcp.types import TextContent, Tool

from ..tool_response import content_success
from .context import ToolContext
from .registry import register


@register(Tool(
    name="detect_rtos",
    description="Detects whether FreeRTOS symbols are available in the current GDB session.",
    inputSchema={"type": "object", "properties": {}}
))
def detect_rtos(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    result = ctx.freertos_inspector.detect()
    return [content_success(result)]


@register(Tool(
    name="read_freertos",
    description="Reads FreeRTOS state by 'what': 'current_task' (pxCurrentTCB), 'tasks' (ready "
                "list: names/priorities/TCB/stack), 'task_lists' (ready/delayed/suspended/"
                "deleted), 'queue' or 'mutex' (needs handle = a Queue_t expression), or 'heap' "
                "(heap_4/5 usage).",
    inputSchema={
        "type": "object",
        "properties": {
            "what": {"type": "string", "enum": ["current_task", "tasks", "task_lists", "queue", "mutex", "heap"]},
            "handle": {"type": "string", "description": "For 'queue'/'mutex': GDB expression for the Queue_t pointer/handle."},
            "max_priorities": {"type": "integer", "description": "tasks/task_lists: priority count override."},
            "max_tasks": {"type": "integer", "description": "tasks/task_lists: max tasks to return."}
        },
        "required": ["what"]
    }
))
def read_freertos(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    freertos_inspector = ctx.freertos_inspector
    what = arguments["what"]
    if what == "current_task":
        result = freertos_inspector.read_current_task()
    elif what == "tasks":
        result = freertos_inspector.read_tasks(
            max_priorities=arguments.get("max_priorities"),
            max_tasks=arguments.get("max_tasks", 64),
        )
    elif what == "task_lists":
        result = freertos_inspector.read_task_lists(
            max_priorities=arguments.get("max_priorities"),
            max_tasks=arguments.get("max_tasks", 128),
        )
    elif what == "queue":
        result = freertos_inspector.read_queue(arguments["handle"])
    elif what == "mutex":
        result = freertos_inspector.read_mutex(arguments["handle"])
    elif what == "heap":
        result = freertos_inspector.read_heap()
    else:
        raise ValueError(f"Unknown read_freertos what '{what}'.")
    return [content_success({"what": what, **result} if isinstance(result, dict) else {"what": what, "result": result})]


@register(Tool(
    name="capture_rtos_snapshot",
    description="Captures FreeRTOS detection, current task, ready tasks, and task-list snapshot.",
    inputSchema={"type": "object", "properties": {}}
))
def capture_rtos_snapshot(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    result = ctx.freertos_inspector.capture_snapshot()
    return [content_success(result)]
