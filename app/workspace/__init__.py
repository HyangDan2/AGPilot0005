"""AGCore R2-W Workspace & RAG engine (independent preview module).

Nothing in this package is imported by the R1.3 runtime unless the integration step in
`docs/r2/WORKSPACE_CONTRACT.md` §8 is performed (one router inclusion in `app/main.py`).
"""
from app.workspace.contracts import WORKSPACE_CONTRACT_VERSION

__all__ = ["WORKSPACE_CONTRACT_VERSION"]
