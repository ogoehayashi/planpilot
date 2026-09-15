from __future__ import annotations
import threading, time
class Metrics:
    def __init__(self): self._lock=threading.Lock(); self._counts={}; self._latency={}
    def observe(self,name,seconds):
        with self._lock: self._counts[name]=self._counts.get(name,0)+1; self._latency[name]=self._latency.get(name,0.0)+seconds
    def snapshot(self):
        with self._lock: return {name:{'count':count,'total_seconds':self._latency.get(name,0.0)} for name,count in self._counts.items()}
METRICS=Metrics()
class Timer:
    def __init__(self,name): self.name=name; self.started=time.perf_counter()
    def __enter__(self): return self
    def __exit__(self,*_): METRICS.observe(self.name,time.perf_counter()-self.started)
