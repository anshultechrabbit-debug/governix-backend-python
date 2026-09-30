from fastapi import Request

from fastapi import Request

from app.core.config import Settings
from app.infrastructure.cache.base import Cache
from app.infrastructure.queue.base import Queue
from app.infrastructure.storage.base import Storage


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_storage(request: Request) -> Storage:
    return request.app.state.storage


def get_cache(request: Request) -> Cache:
    return request.app.state.cache


def get_queue(request: Request) -> Queue:
    return request.app.state.queue
