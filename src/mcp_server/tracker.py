import threading
import time


class VariableTracker:
    def __init__(self, gdb_client):
        self.gdb_client = gdb_client
        self.tracking = False
        self._thread = None
        self.data = []
        self.variable = None
        self.interval = 0.1

    def _track_loop(self):
        while self.tracking:
            start = time.time()
            try:
                # Live memory read via GDB
                resp = self.gdb_client.read_variable(self.variable)
                self.data.append({
                    "time": start,
                    "response": resp
                })
            except Exception as e:
                self.data.append({"time": start, "error": str(e)})
            
            elapsed = time.time() - start
            sleep_time = max(0, self.interval - elapsed)
            time.sleep(sleep_time)

    def start(self, variable: str, interval_ms: int):
        if self.tracking:
            self.stop()
        self.variable = variable
        self.interval = interval_ms / 1000.0
        self.data = []
        self.tracking = True
        self._thread = threading.Thread(target=self._track_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.tracking = False
        if self._thread:
            self._thread.join()
            
    def get_data(self):
        return self.data
