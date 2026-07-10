"""
Runs an RQ worker for the configured USEP queue.
"""

import argparse
import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
from rq import Worker
from usep_indexer_app.lib.queue_support import get_queue


def main() -> None:
    """
    Loads Django settings and runs the configured RQ worker.

    Called by: dundermain
    """
    parser = argparse.ArgumentParser(description='Run the USEP RQ worker')
    parser.add_argument('--burst', action='store_true', help='Exit after all currently queued jobs finish')
    args = parser.parse_args()

    django.setup()
    queue = get_queue()
    worker = Worker([queue], connection=queue.connection)
    worker.work(burst=args.burst)
    return


if __name__ == '__main__':
    main()
