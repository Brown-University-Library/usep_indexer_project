import datetime
import json
import logging

from django.conf import settings as project_settings
from django.http import HttpRequest, HttpResponse, HttpResponseNotFound, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods
from usep_indexer_app.lib import orphans, payloads, processing_check_helper, spool, version_helper
from usep_indexer_app.lib.auth import basic_auth_required


log = logging.getLogger(__name__)


@require_GET
def processing_check(request: HttpRequest) -> JsonResponse:
    """
    Reports filesystem-queue processor freshness and backlog state.

    Called by: config.urls.urlpatterns
    """
    request_started = datetime.datetime.now()
    request_ip = request.META.get('REMOTE_ADDR', 'ip_not_available')
    if not processing_check_helper.validate_request_source(request_ip):
        return JsonResponse({'detail': '404 / Not Found'}, status=404)

    health = processing_check_helper.check_processing()
    context = processing_check_helper.make_context(request, request_started, health)
    return JsonResponse(context, json_dumps_params={'indent': 2, 'sort_keys': True})


@require_GET
def info(request: HttpRequest) -> JsonResponse:
    """
    Returns service metadata.

    Called by: config.urls.urlpatterns
    """
    del request
    context = {
        'datetime': str(datetime.datetime.now()),
        'info': project_settings.README_URL,
    }
    return JsonResponse(context, json_dumps_params={'indent': 2})


@require_GET
@basic_auth_required
def list_orphans(request: HttpRequest) -> HttpResponse:
    """
    Lists Solr IDs that have no matching web-served inscription file.

    Called by: config.urls.urlpatterns
    """
    start_time = datetime.datetime.now()
    orphan_ids = orphans.prep_orphan_list()
    request.session['ids_to_delete'] = orphan_ids
    context = orphans.prep_context(orphan_ids, reverse('orphan_handler_url'), start_time)

    if request.GET.get('format') == 'json':
        response: HttpResponse = JsonResponse(context, json_dumps_params={'indent': 2})
    else:
        response = render(request, 'orphan_list.html', context)
    return response


@require_GET
@basic_auth_required
def delete_orphans(request: HttpRequest) -> HttpResponse:
    """
    Deletes the orphan IDs saved by the preceding list-orphans request.

    Called by: config.urls.urlpatterns
    """
    action = request.GET.get('action_button')
    response: HttpResponse
    if action == 'No':
        response = HttpResponse('no orphans deleted')
    elif action == 'Yes':
        ids_to_delete = request.session.get('ids_to_delete', [])
        errors = orphans.run_deletes(ids_to_delete)
        if errors:
            response = HttpResponse('problems deleting some orphans; see logs for details')
        else:
            response = HttpResponse('all orphans deleted')
    else:
        response = HttpResponse('bad-request', status=400)
    return response


@require_GET
@basic_auth_required
def reindex_all(request: HttpRequest) -> HttpResponse:
    """
    Saves a durable full pull, copy, and reindex request.

    Called by: config.urls.urlpatterns
    """
    del request
    try:
        spool.write_event(project_settings.SPOOL_ROOT_PATH, 'full_reindex')
        response = HttpResponse('pull and reindex initiated.')
    except OSError:
        log.exception('Unable to durably save the full-reindex request.')
        response = HttpResponse('unable to queue full reindex', status=503)
    return response


@csrf_exempt
@require_http_methods(['GET', 'POST'])
@basic_auth_required
def handle_github_push(request: HttpRequest) -> HttpResponse:
    """
    Accepts the legacy GitHub webhook contract and durably saves processing.

    Called by: config.urls.urlpatterns
    """
    request_id = request.headers.get('X-GitHub-Delivery')
    logged_request_id = request_id or 'not_provided'
    log.info(f'github request received; request_id, ``{logged_request_id}``; request_method, ``{request.method}``')
    log.debug(
        f'request_path, ``{request.path}``; remote_addr, ``{request.META.get("REMOTE_ADDR", "unknown")}``; '
        f'body_bytes, ``{len(request.body)}``'
    )
    response = HttpResponse('received')
    if request.body or request.path.rstrip('/').endswith('/force'):
        files_to_process = payloads.prepare_files_to_process(request.body)
        log.debug(
            f'files_updated, ``{files_to_process["files_updated"]}``; files_removed, ``{files_to_process["files_removed"]}``'
        )
        try:
            spool.write_event(
                project_settings.SPOOL_ROOT_PATH,
                'incremental',
                files_updated=files_to_process['files_updated'],
                files_removed=files_to_process['files_removed'],
                request_id=request_id,
            )
        except OSError:
            log.exception('Unable to durably save the GitHub push event.')
            response = HttpResponse('unable to queue event', status=503)
    else:
        log.debug(f'queue_action, ``skipped``; request_id, ``{logged_request_id}``; reason, ``no request body``')
    return response


@require_GET
def error_check(request: HttpRequest) -> HttpResponse:
    """
    Raises an intentional development exception or returns 404 in production.

    Called by: config.urls.urlpatterns
    """
    del request
    if project_settings.DEBUG is True:
        raise Exception('Raising intentional exception to check email-admins-on-error functionality.')
    return HttpResponseNotFound('<div>404 / Not Found</div>')


@require_GET
def version(request: HttpRequest) -> HttpResponse:
    """
    Returns branch and commit data.

    Called by: config.urls.urlpatterns
    """
    request_started = datetime.datetime.now()
    branch, commit = version_helper.get_branch_and_commit()
    context = version_helper.make_context(request, request_started, f'{branch} {commit}')
    output = json.dumps(context, sort_keys=True, indent=2)
    return HttpResponse(output, content_type='application/json; charset=utf-8')
