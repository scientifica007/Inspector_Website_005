from django.urls import path

from . import views

app_name = "visits"

urlpatterns = [
    path("", views.visit_list, name="list"),
    path("new/", views.visit_create, name="create"),
    path("<int:pk>/", views.visit_detail, name="detail"),
    path("<int:pk>/edit/", views.visit_edit, name="edit"),
    path("<int:pk>/delete/", views.visit_delete, name="delete"),
    path("<int:pk>/scope/", views.visit_scope, name="scope"),
    path("<int:pk>/scope/<int:node_pk>/select/", views.scope_select, name="scope_select"),
    path("<int:pk>/scope/<int:node_pk>/deselect/", views.scope_deselect, name="scope_deselect"),
    path("<int:pk>/scope/<int:node_pk>/exclude/", views.scope_exclude, name="scope_exclude"),
    path("<int:pk>/scope/<int:node_pk>/restore/", views.scope_restore, name="scope_restore"),
    path("<int:pk>/scope/local/", views.local_node_create, name="local_node_create"),
    path("<int:pk>/execute/", views.visit_execute, name="execute"),
    path("<int:pk>/execute/<int:node_pk>/result/", views.record_item, name="record_item"),
    path("<int:pk>/execute/<int:node_pk>/value/", views.record_value, name="record_value"),
    path("<int:pk>/complete/", views.complete_visit_view, name="complete"),
    path("<int:pk>/export/", views.visit_export, name="export"),
]