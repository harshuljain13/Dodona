"""Load generators. Run them ON THE GPU BOX, not over the ssh tunnel.

A tunnel puts your WAN round-trip inside every TTFT and serialises concurrency
through one TCP stream, so numbers taken from a laptop measure your internet
rather than the cluster. `make load` runs these on the box for that reason.
"""
