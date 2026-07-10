"""
Runs the Django test suite without requiring project environment settings.
"""

import argparse
import os
import sys
from pathlib import Path

os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings_run_tests'

import django
from django.conf import settings
from django.test.utils import get_runner


def main() -> None:
    """
    Discovers and runs Django tests.

    Called by: dundermain
    """
    parser = argparse.ArgumentParser(description='Run webapp tests')
    parser.add_argument('-v', '--verbose', action='store_true', help='Use unittest verbosity 2')
    parser.add_argument('targets', nargs='*', help='Optional dotted test targets')
    args = parser.parse_args()

    webapp_root = Path(__file__).parent
    sys.path.insert(0, str(webapp_root))
    os.chdir(webapp_root)
    django.setup()

    test_runner_class = get_runner(settings)
    test_runner = test_runner_class(verbosity=2 if args.verbose else 1, interactive=False)
    failures = test_runner.run_tests(list(args.targets))
    sys.exit(0 if failures == 0 else 1)


if __name__ == '__main__':
    main()
