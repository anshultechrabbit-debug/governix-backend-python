import importlib

# Modules whose @task handlers the worker must register (filled from Phase 4 on).
TASK_MODULES: list[str] = [
    "app.workers.extraction.tasks",
    "app.workers.ocr.tasks",
    "app.workers.structure.tasks",
    "app.workers.analysis.tasks",
    "app.workers.chunking.tasks",
    "app.workers.embeddings.tasks",
    "app.workers.validation.tasks",
    "app.workers.platform.tasks",
]


def load_tasks() -> None:
    for module in TASK_MODULES:
        importlib.import_module(module)
