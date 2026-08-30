"""Minimal custom strategy accepted by QuantSieve's local sandbox."""


def generate_signals(data):
    fast = data["close"].rolling(20).mean()
    slow = data["close"].rolling(60).mean()
    return (fast > slow).astype(float)
