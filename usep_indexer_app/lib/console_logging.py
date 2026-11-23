"""
Formats command-line messages independently of Django's application logging.
"""

import datetime


def format_message(message: str, level: str = 'INFO') -> str:
    """
    Adds a local timestamp and severity using the application log's date format.

    Called by: manage.main(), management.commands.process_spool.Command.handle()
    """
    timestamp: str = datetime.datetime.now().strftime('%d/%b/%Y %H:%M:%S')
    formatted_message: str = f'[{timestamp}] {level} {message}'
    return formatted_message
