"""Bounded worker lanes: each chat always uses the same FIFO queue."""

import queue
import threading
import traceback


class Dispatcher:
    def __init__(self, workers: int, capacity: int):
        self.queues = [queue.Queue(maxsize=capacity) for _ in range(workers)]
        self.lock = threading.Lock()
        self.scheduled = set()
        self.threads = []
        self.closing = threading.Event()

    def submit(self, chat_id, token, fn) -> bool:
        with self.lock:
            if self.closing.is_set():
                return False
            if token in self.scheduled:
                return True
            lane = self.queues[int(chat_id) % len(self.queues)]
            try:
                lane.put_nowait((token, fn))
            except queue.Full:
                return False
            self.scheduled.add(token)
            if not self.threads:
                for work_queue in self.queues:
                    thread = threading.Thread(target=self._work, args=(work_queue,), daemon=True)
                    self.threads.append(thread)
                    thread.start()
            return True

    def _work(self, lane):
        while not self.closing.is_set() or not lane.empty():
            try:
                token, fn = lane.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                fn()
            except Exception:
                traceback.print_exc()
            finally:
                with self.lock:
                    self.scheduled.discard(token)
                lane.task_done()

    def close(self):
        self.closing.set()
        for thread in self.threads:
            thread.join()
