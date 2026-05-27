from collections import defaultdict
from time import perf_counter


class Metrics:
    def __init__(self):
        self.counters: dict[str, int] = defaultdict(int)
        self.gauges: dict[str, float] = defaultdict(float)
        self.timings: dict[str, list[float]] = defaultdict(list)

    def inc(self, name: str, value: int = 1) -> None:
        self.counters[name] += value

    def gauge(self, name: str, value: float) -> None:
        self.gauges[name] = value

    def observe_ms(self, name: str, value: float) -> None:
        values = self.timings[name]
        values.append(value)
        if len(values) > 1000:
            del values[:500]

    def timer(self, name: str):
        return _Timer(self, name)

    def prometheus(self) -> str:
        lines: list[str] = []
        for name, value in sorted(self.counters.items()):
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {value}")
        for name, value in sorted(self.gauges.items()):
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {value}")
        for name, values in sorted(self.timings.items()):
            if not values:
                continue
            lines.append(f"# TYPE {name}_ms summary")
            lines.append(f"{name}_ms_count {len(values)}")
            lines.append(f"{name}_ms_sum {sum(values):.3f}")
            lines.append(f"{name}_ms_avg {sum(values) / len(values):.3f}")
            lines.append(f"{name}_ms_max {max(values):.3f}")
        return "\n".join(lines) + "\n"


class _Timer:
    def __init__(self, metrics: Metrics, name: str):
        self.metrics = metrics
        self.name = name
        self.start = 0.0

    def __enter__(self):
        self.start = perf_counter()
        return self

    def __exit__(self, *_):
        self.metrics.observe_ms(self.name, (perf_counter() - self.start) * 1000)
