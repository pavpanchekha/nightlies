def format_time(ts: float) -> str:
    t = float(ts)
    if t < 120:
        return f"{t:.1f}s"
    elif t < 120 * 60:
        return f"{t / 60:.1f}m"
    else:
        return f"{t / 60 / 60:.1f}h"
