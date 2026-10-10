
import threading
from itertools import cycle

class ProxyManager:
    def __init__(self, proxy_file="proxies.txt"):
        self.proxy_file = proxy_file
        self.proxies = []
        self.proxy_cycle = None
        self.lock = threading.Lock()
        self.load_proxies()

    def load_proxies(self):
        with self.lock:
            try:
                with open(self.proxy_file, "r") as f:
                    self.proxies = [line.strip() for line in f if line.strip()]
                self.proxy_cycle = cycle(self.proxies) if self.proxies else None
            except FileNotFoundError:
                self.proxies = []
                self.proxy_cycle = None

    def set_proxies(self, proxies):
        with self.lock:
            self.proxies = proxies
            self.proxy_cycle = cycle(self.proxies) if self.proxies else None

    def get_next_proxy(self):
        with self.lock:
            if not self.proxy_cycle:
                return None
            return next(self.proxy_cycle)

    def get_proxy_count(self):
        with self.lock:
            return len(self.proxies)

# Global instance
proxy_manager = ProxyManager()
