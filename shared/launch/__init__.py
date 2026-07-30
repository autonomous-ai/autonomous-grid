"""Launch targets: the apps `grid launch` starts for the user, pointed at their active grid.

A **launch target** (CONTEXT-MAP.md) is named by the user rather than configured by them — they say
`claude`, not an endpoint and a credential. This package owns the targets and the one OS-facing seam
they act through; resolving *which* grid, and its token and relay base, is the CLI's job
(`cli/launch.py`), so nothing here imports `cli`. See ADR 0028.
"""
