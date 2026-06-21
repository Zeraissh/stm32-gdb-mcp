from mcp_server.freertos_inspector import FreeRTOSInspector


class FakeGdbClient:
    def __init__(self, values):
        self.values = values
        self.queries = []

    def read_variable(self, expression):
        self.queries.append(expression)
        if expression not in self.values:
            return [{"type": "result", "message": "error", "payload": {"msg": "No symbol"}}]
        return [{"type": "result", "message": "done", "payload": {"value": self.values[expression]}}]


def test_detect_rtos_reports_freertos_when_core_symbols_exist():
    client = FakeGdbClient({
        "pxCurrentTCB": "0x20001000 <pxCurrentTCB>",
        "uxCurrentNumberOfTasks": "3",
    })
    inspector = FreeRTOSInspector(client)

    result = inspector.detect()

    assert result["rtos"] == "FreeRTOS"
    assert result["detected"] is True
    assert result["task_count"] == 3


def test_read_current_task_returns_tcb_fields():
    client = FakeGdbClient({
        "pxCurrentTCB": "0x20001000",
        "((TCB_t *)0x20001000)->pcTaskName": '"Control"',
        "((TCB_t *)0x20001000)->uxPriority": "5",
        "((TCB_t *)0x20001000)->pxTopOfStack": "0x20001f00",
        "((TCB_t *)0x20001000)->pxStack": "0x20001800",
    })
    inspector = FreeRTOSInspector(client)

    task = inspector.read_current_task()

    assert task["name"] == "Control"
    assert task["tcb"] == "0x20001000"
    assert task["priority"] == 5
    assert task["stack"]["top"] == "0x20001f00"
    assert task["stack"]["base"] == "0x20001800"


def test_read_tasks_walks_ready_lists_by_priority():
    values = {
        "configMAX_PRIORITIES": "2",
        "pxReadyTasksLists[0].uxNumberOfItems": "0",
        "pxReadyTasksLists[1].uxNumberOfItems": "1",
        "&pxReadyTasksLists[1].xListEnd": "0x20003000",
        "pxReadyTasksLists[1].xListEnd.xNext": "0x20003100",
        "((ListItem_t *)0x20003100)->pvOwner": "0x20001000",
        "((ListItem_t *)0x20003100)->pxNext": "0x20003000",
        "((TCB_t *)0x20001000)->pcTaskName": '"Worker"',
        "((TCB_t *)0x20001000)->uxPriority": "1",
        "((TCB_t *)0x20001000)->pxTopOfStack": "0x20001f00",
        "((TCB_t *)0x20001000)->pxStack": "0x20001800",
    }
    inspector = FreeRTOSInspector(FakeGdbClient(values))

    tasks = inspector.read_tasks()

    assert tasks["source"] == "ready_lists"
    assert tasks["tasks"] == [
        {
            "name": "Worker",
            "tcb": "0x20001000",
            "priority": 1,
            "state": "Ready",
            "stack": {"top": "0x20001f00", "base": "0x20001800"},
            "list_item": "0x20003100",
        }
    ]


def test_read_task_lists_includes_delayed_and_suspended_tasks():
    values = {
        "configMAX_PRIORITIES": "1",
        "pxReadyTasksLists[0].uxNumberOfItems": "0",
        "xDelayedTaskList1.uxNumberOfItems": "1",
        "&xDelayedTaskList1.xListEnd": "0x20004000",
        "xDelayedTaskList1.xListEnd.xNext": "0x20004100",
        "((ListItem_t *)0x20004100)->pvOwner": "0x20001000",
        "((ListItem_t *)0x20004100)->pxNext": "0x20004000",
        "((TCB_t *)0x20001000)->pcTaskName": '"Sleepy"',
        "((TCB_t *)0x20001000)->uxPriority": "2",
        "((TCB_t *)0x20001000)->pxTopOfStack": "0x20001f00",
        "((TCB_t *)0x20001000)->pxStack": "0x20001800",
        "xDelayedTaskList2.uxNumberOfItems": "0",
        "xSuspendedTaskList.uxNumberOfItems": "1",
        "&xSuspendedTaskList.xListEnd": "0x20005000",
        "xSuspendedTaskList.xListEnd.xNext": "0x20005100",
        "((ListItem_t *)0x20005100)->pvOwner": "0x20002000",
        "((ListItem_t *)0x20005100)->pxNext": "0x20005000",
        "((TCB_t *)0x20002000)->pcTaskName": '"Paused"',
        "((TCB_t *)0x20002000)->uxPriority": "1",
        "((TCB_t *)0x20002000)->pxTopOfStack": "0x20002f00",
        "((TCB_t *)0x20002000)->pxStack": "0x20002800",
    }
    inspector = FreeRTOSInspector(FakeGdbClient(values))

    lists = inspector.read_task_lists()

    assert lists["Delayed"][0]["name"] == "Sleepy"
    assert lists["Delayed"][0]["state"] == "Delayed"
    assert lists["Suspended"][0]["name"] == "Paused"
    assert lists["Suspended"][0]["state"] == "Suspended"


def test_read_queue_returns_capacity_messages_and_waiting_tasks():
    values = {
        "myQueue": "0x20006000",
        "((Queue_t *)0x20006000)->uxMessagesWaiting": "2",
        "((Queue_t *)0x20006000)->uxLength": "8",
        "((Queue_t *)0x20006000)->uxItemSize": "4",
        "((Queue_t *)0x20006000)->pcHead": "0x20006100",
        "((Queue_t *)0x20006000)->pcWriteTo": "0x20006108",
        "((Queue_t *)0x20006000)->xTasksWaitingToReceive.uxNumberOfItems": "1",
        "&((Queue_t *)0x20006000)->xTasksWaitingToReceive.xListEnd": "0x20007000",
        "((Queue_t *)0x20006000)->xTasksWaitingToReceive.xListEnd.xNext": "0x20007100",
        "((ListItem_t *)0x20007100)->pvOwner": "0x20001000",
        "((ListItem_t *)0x20007100)->pxNext": "0x20007000",
        "((TCB_t *)0x20001000)->pcTaskName": '"Consumer"',
        "((TCB_t *)0x20001000)->uxPriority": "3",
        "((TCB_t *)0x20001000)->pxTopOfStack": "0x20001f00",
        "((TCB_t *)0x20001000)->pxStack": "0x20001800",
        "((Queue_t *)0x20006000)->xTasksWaitingToSend.uxNumberOfItems": "0",
    }
    inspector = FreeRTOSInspector(FakeGdbClient(values))

    queue = inspector.read_queue("myQueue")

    assert queue["address"] == "0x20006000"
    assert queue["messages_waiting"] == 2
    assert queue["length"] == 8
    assert queue["item_size"] == 4
    assert queue["waiting_to_receive"][0]["name"] == "Consumer"
    assert queue["waiting_to_receive"][0]["state"] == "WaitingToReceive"
    assert queue["waiting_to_send"] == []


def test_read_mutex_returns_owner_and_recursive_count():
    values = {
        "myMutex": "0x20008000",
        "((Queue_t *)0x20008000)->uxMessagesWaiting": "0",
        "((Queue_t *)0x20008000)->uxLength": "1",
        "((Queue_t *)0x20008000)->uxItemSize": "0",
        "((Queue_t *)0x20008000)->pcHead": "0x0",
        "((Queue_t *)0x20008000)->pcWriteTo": "0x0",
        "((Queue_t *)0x20008000)->xTasksWaitingToReceive.uxNumberOfItems": "0",
        "((Queue_t *)0x20008000)->xTasksWaitingToSend.uxNumberOfItems": "0",
        "((Queue_t *)0x20008000)->u.xSemaphore.xMutexHolder": "0x20001000",
        "((Queue_t *)0x20008000)->u.xSemaphore.uxRecursiveCallCount": "2",
        "((TCB_t *)0x20001000)->pcTaskName": '"Owner"',
        "((TCB_t *)0x20001000)->uxPriority": "4",
        "((TCB_t *)0x20001000)->pxTopOfStack": "0x20001f00",
        "((TCB_t *)0x20001000)->pxStack": "0x20001800",
    }
    inspector = FreeRTOSInspector(FakeGdbClient(values))

    mutex = inspector.read_mutex("myMutex")

    assert mutex["address"] == "0x20008000"
    assert mutex["owner"]["name"] == "Owner"
    assert mutex["owner"]["state"] == "MutexOwner"
    assert mutex["recursive_call_count"] == 2
    assert mutex["queue"]["item_size"] == 0


def test_read_heap_returns_free_minimum_and_total_bytes():
    values = {
        "xFreeBytesRemaining": "1536",
        "xMinimumEverFreeBytesRemaining": "512",
        "configTOTAL_HEAP_SIZE": "4096",
    }
    inspector = FreeRTOSInspector(FakeGdbClient(values))

    heap = inspector.read_heap()

    assert heap == {
        "free_bytes": 1536,
        "minimum_ever_free_bytes": 512,
        "total_heap_size": 4096,
        "used_bytes": 2560,
        "minimum_ever_used_bytes": 3584,
    }
