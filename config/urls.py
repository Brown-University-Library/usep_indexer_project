from django.urls import path
from usep_indexer_app import views


urlpatterns = [
    path('', views.handle_github_push, name='root_url'),
    path('force/', views.handle_github_push, name='force_url'),
    path('reindex_all/', views.reindex_all, name='reindex_all_url'),
    path('list_orphans/', views.list_orphans, name='list_orphans_url'),
    path('orphan_handler/', views.delete_orphans, name='orphan_handler_url'),
    path('daemon_check/', views.daemon_check, name='daemon_check_url'),
    path('info/', views.info, name='info_url'),
    path('version/', views.version, name='version_url'),
    path('error_check/', views.error_check, name='error_check_url'),
]
