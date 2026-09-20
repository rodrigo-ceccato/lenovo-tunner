"""Separate, opt-in CPU and CUDA stress workloads for Tunner."""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import sys


def cpu_worker() -> None:
    """Keep one CPU core busy without sharing state with other workers."""
    value = os.urandom(32)
    while True:
        value = hashlib.sha256(value).digest()


def stress_cpu() -> None:
    """Use one process per logical CPU so Python's GIL does not limit load."""
    workers = [multiprocessing.Process(target=cpu_worker) for _ in range(os.cpu_count() or 1)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()


def stress_gpu() -> None:
    """Continuously multiply CUDA matrices with CuPy until this process stops."""
    try:
        import cupy as cp
    except ImportError as error:
        raise SystemExit(
            "GPU stress requires CuPy. Install the CUDA-matched wheel, for example: "
            "pip install cupy-cuda12x"
        ) from error

    if cp.cuda.runtime.getDeviceCount() < 1:
        raise SystemExit("GPU stress requires a CUDA-capable GPU visible to CuPy.")
    matrix = cp.random.random((4096, 4096), dtype=cp.float32)
    while True:
        matrix = matrix @ matrix
        cp.cuda.Stream.null.synchronize()


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in {"cpu", "gpu"}:
        raise SystemExit("Usage: stress.py {cpu|gpu}")
    {"cpu": stress_cpu, "gpu": stress_gpu}[sys.argv[1]]()


if __name__ == "__main__":
    main()
