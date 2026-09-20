from django.urls import path

from . import views

app_name = "catalog"

urlpatterns = [
    path("", views.reference_list, name="list"),
    path("new/shared/", views.reference_create_shared, name="create_shared"),
    path("new/private/", views.reference_create_private, name="create_private"),
    path("submissions/", views.submission_list, name="submission_list"),
    path("submissions/<int:pk>/", views.submission_detail, name="submission_detail"),
    path("submissions/<int:pk>/approve/", views.submission_approve, name="submission_approve"),
    path("submissions/<int:pk>/reject/", views.submission_reject, name="submission_reject"),
    path("<int:pk>/", views.reference_detail, name="detail"),
    path("<int:pk>/edit/", views.reference_edit, name="edit"),
    path("<int:pk>/delete/", views.reference_delete, name="delete"),
    path("<int:pk>/restore/", views.reference_restore, name="restore"),
    path("<int:pk>/submit/", views.submission_create, name="submit"),
    path("<int:pk>/nodes/new/", views.node_create, name="node_create"),
    path("<int:pk>/nodes/<int:node_pk>/edit/", views.node_edit, name="node_edit"),
    path("<int:pk>/nodes/<int:node_pk>/delete/", views.node_delete, name="node_delete"),
]