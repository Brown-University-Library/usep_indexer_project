from django.conf import settings
from redis import Redis
from rq import Queue


def get_redis_connection() -> Redis:
    """
    Builds a Redis connection from project settings.

    Called by: get_queue(), run_worker.main()
    """
    connection = Redis.from_url(settings.REDIS_URL, protocol=2)
    return connection


def get_queue() -> Queue:
    """
    Builds the configured RQ queue.

    Called by: enqueue_call(), daemon.check_daemon(), run_worker.main()
    """
    queue = Queue(settings.RQ_QUEUE_NAME, connection=get_redis_connection())
    return queue


def enqueue_call(function_path: str, kwargs: dict[str, object]) -> str:
    """
    Enqueues a dotted function path and returns its RQ job ID.

    Called by: views and queue job runners
    """
    job = get_queue().enqueue_call(func=function_path, kwargs=kwargs)
    return job.id
