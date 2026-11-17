#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""

import os
import sys

import django  # for the "in case you're curious about versions" code

from usep_indexer_app.lib import console_logging


def main() -> None:
    """
    Runs Django administrative tasks.

    Called by: dundermain
    """
    ## in case you're curious about versions -- feel free to comment out
    if os.environ.get('RUN_MAIN') != 'true':
        major, minor, micro = sys.version_info[:3]
        print(console_logging.format_message(f'using python version, ``{major}.{minor}.{micro}``'), flush=True)
        print(console_logging.format_message(f'using django version, ``{django.get_version()}``'), flush=True)

    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            'available on your PYTHONPATH environment variable? Did you '
            'forget to activate a virtual environment?'
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
