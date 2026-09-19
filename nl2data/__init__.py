"""nl2data: query large Excel/Access datasets with natural language."""

import os

# OpenBLAS reserves per-thread buffers at import time; on many-core machines
# under memory pressure the default (threads == core count) can fail to
# allocate. A single thread is plenty for our ingestion workloads.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

__version__ = "0.1.0"
