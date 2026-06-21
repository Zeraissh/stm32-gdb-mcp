import re


class FreeRTOSInspector:
    def __init__(self, gdb_client):
        self.gdb_client = gdb_client

    def detect(self) -> dict:
        current_tcb = self._read_value("pxCurrentTCB")
        task_count = self._read_int("uxCurrentNumberOfTasks")
        detected = current_tcb is not None and task_count is not None
        return {
            "rtos": "FreeRTOS" if detected else None,
            "detected": detected,
            "task_count": task_count,
            "symbols": {
                "pxCurrentTCB": current_tcb,
                "uxCurrentNumberOfTasks": task_count,
            },
        }

    def read_current_task(self) -> dict:
        tcb = self._read_pointer("pxCurrentTCB")
        if not tcb:
            raise RuntimeError("FreeRTOS symbol pxCurrentTCB is not available")
        return self._read_tcb(tcb)

    def read_tasks(self, max_priorities: int | None = None, max_tasks: int = 64) -> dict:
        priority_count = max_priorities or self._read_int("configMAX_PRIORITIES") or 8
        tasks = []

        for priority in range(priority_count):
            tasks.extend(self._walk_list(f"pxReadyTasksLists[{priority}]", "Ready", max_tasks - len(tasks)))
            if len(tasks) >= max_tasks:
                break

        return {
            "source": "ready_lists",
            "priority_count": priority_count,
            "tasks": tasks,
        }

    def read_task_lists(self, max_priorities: int | None = None, max_tasks: int = 128) -> dict:
        ready = self.read_tasks(max_priorities=max_priorities, max_tasks=max_tasks)["tasks"]
        remaining = max_tasks - len(ready)
        delayed = []
        delayed.extend(self._walk_list("xDelayedTaskList1", "Delayed", remaining))
        remaining = max_tasks - len(ready) - len(delayed)
        delayed.extend(self._walk_list("xDelayedTaskList2", "Delayed", remaining))
        remaining = max_tasks - len(ready) - len(delayed)
        suspended = self._walk_list("xSuspendedTaskList", "Suspended", remaining)
        remaining = max_tasks - len(ready) - len(delayed) - len(suspended)
        terminated = self._walk_list("xTasksWaitingTermination", "Deleted", remaining)

        return {
            "Ready": ready,
            "Delayed": delayed,
            "Suspended": suspended,
            "Deleted": terminated,
        }

    def read_queue(self, queue_expression: str) -> dict:
        address = self._read_pointer(queue_expression)
        if not address:
            raise RuntimeError(f"Could not resolve FreeRTOS queue expression: {queue_expression}")

        queue = f"((Queue_t *){address})"
        return {
            "expression": queue_expression,
            "address": address,
            "messages_waiting": self._read_int(f"{queue}->uxMessagesWaiting"),
            "length": self._read_int(f"{queue}->uxLength"),
            "item_size": self._read_int(f"{queue}->uxItemSize"),
            "storage": {
                "head": self._read_pointer(f"{queue}->pcHead"),
                "write_to": self._read_pointer(f"{queue}->pcWriteTo"),
            },
            "waiting_to_receive": self._walk_list(f"{queue}->xTasksWaitingToReceive", "WaitingToReceive", 64),
            "waiting_to_send": self._walk_list(f"{queue}->xTasksWaitingToSend", "WaitingToSend", 64),
        }

    def read_mutex(self, mutex_expression: str) -> dict:
        queue = self.read_queue(mutex_expression)
        queue_struct = f"((Queue_t *){queue['address']})"
        owner_tcb = self._read_pointer(f"{queue_struct}->u.xSemaphore.xMutexHolder")
        owner = None
        if owner_tcb:
            owner = self._read_tcb(owner_tcb)
            owner["state"] = "MutexOwner"

        return {
            "expression": mutex_expression,
            "address": queue["address"],
            "owner": owner,
            "recursive_call_count": self._read_int(f"{queue_struct}->u.xSemaphore.uxRecursiveCallCount"),
            "queue": queue,
        }

    def read_heap(self) -> dict:
        free_bytes = self._read_int("xFreeBytesRemaining")
        minimum_ever_free = self._read_int("xMinimumEverFreeBytesRemaining")
        total_heap = self._read_int("configTOTAL_HEAP_SIZE")
        heap = {
            "free_bytes": free_bytes,
            "minimum_ever_free_bytes": minimum_ever_free,
            "total_heap_size": total_heap,
        }
        if total_heap is not None and free_bytes is not None:
            heap["used_bytes"] = total_heap - free_bytes
        if total_heap is not None and minimum_ever_free is not None:
            heap["minimum_ever_used_bytes"] = total_heap - minimum_ever_free
        return heap

    def capture_snapshot(self) -> dict:
        snapshot = {
            "detection": self.detect(),
        }
        if snapshot["detection"]["detected"]:
            snapshot["current_task"] = self.read_current_task()
            snapshot["tasks"] = self.read_tasks()
            snapshot["task_lists"] = self.read_task_lists()
            snapshot["heap"] = self.read_heap()
        return snapshot

    def _read_tcb(self, tcb: str) -> dict:
        return {
            "name": self._read_string(f"((TCB_t *){tcb})->pcTaskName") or "<unknown>",
            "tcb": tcb,
            "priority": self._read_int(f"((TCB_t *){tcb})->uxPriority"),
            "stack": {
                "top": self._read_pointer(f"((TCB_t *){tcb})->pxTopOfStack"),
                "base": self._read_pointer(f"((TCB_t *){tcb})->pxStack"),
            },
        }

    def _walk_list(self, list_expression: str, state: str, max_items: int) -> list[dict]:
        if max_items <= 0:
            return []

        count = self._read_int(f"{list_expression}.uxNumberOfItems") or 0
        if count <= 0:
            return []

        sentinel = self._read_pointer(f"&{list_expression}.xListEnd")
        item = self._read_pointer(f"{list_expression}.xListEnd.xNext")
        tasks = []
        seen = set()

        for _ in range(min(count, max_items)):
            if not item or item == sentinel or item in seen:
                break
            seen.add(item)
            owner = self._read_pointer(f"((ListItem_t *){item})->pvOwner")
            if owner:
                task = self._read_tcb(owner)
                task["state"] = state
                task["list_item"] = item
                tasks.append(task)
            item = self._read_pointer(f"((ListItem_t *){item})->pxNext")

        return tasks

    def _read_value(self, expression: str):
        response = self.gdb_client.read_variable(expression)
        for record in response:
            if record.get("message") == "error":
                continue
            payload = record.get("payload")
            if isinstance(payload, dict) and "value" in payload:
                return payload["value"]
            if isinstance(payload, str):
                return payload
        return None

    def _read_int(self, expression: str):
        value = self._read_value(expression)
        if value is None:
            return None
        match = re.search(r"0x[0-9a-fA-F]+|\d+", str(value))
        if not match:
            return None
        return int(match.group(0), 0)

    def _read_pointer(self, expression: str):
        value = self._read_value(expression)
        if value is None:
            return None
        match = re.search(r"0x[0-9a-fA-F]+", str(value))
        if match:
            return match.group(0).lower()
        return None

    def _read_string(self, expression: str):
        value = self._read_value(expression)
        if value is None:
            return None
        text = str(value).strip()
        if text.startswith('"') and text.endswith('"'):
            text = text[1:-1]
        return text.replace("\\000", "").strip()
