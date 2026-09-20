from django.urls import path

from . import views

app_name = "governance"

urlpatterns = [
    path("guides/", views.guide_list, name="guide_list"),
    path("guides/new/", views.guide_create, name="guide_create"),
    path("guides/<int:pk>/", views.guide_detail, name="guide_detail"),
    path("guides/<int:pk>/edit/", views.guide_edit, name="guide_edit"),
    path("guides/<int:pk>/delete/", views.guide_delete, name="guide_delete"),
    path("guides/<int:pk>/publish/", views.guide_publish, name="guide_publish"),
    path("guides/<int:pk>/targets/add/", views.guide_target_add, name="guide_target_add"),
    path(
        "guides/<int:pk>/targets/<int:target_pk>/remove/",
        views.guide_target_remove,
        name="guide_target_remove",
    ),
    path("visits/<int:pk>/guides/<int:guide_pk>/apply/", views.guide_apply, name="guide_apply"),
    path("assignments/", views.assignment_list, name="assignment_list"),
    path("assignments/mine/", views.my_assignments, name="my_assignments"),
    path(
        "assignments/visit/<int:visit_pk>/new/",
        views.assignment_create,
        name="assignment_create",
    ),
    path("assignments/<int:pk>/", views.assignment_detail, name="assignment_detail"),
    path(
        "assignments/<int:pk>/entries/add/",
        views.assignment_entry_add,
        name="assignment_entry_add",
    ),
    path(
        "assignments/<int:pk>/entries/<int:entry_pk>/remove/",
        views.assignment_entry_remove,
        name="assignment_entry_remove",
    ),
    path("assignments/<int:pk>/issue/", views.assignment_issue, name="assignment_issue"),
    path("assignments/<int:pk>/revoke/", views.assignment_revoke, name="assignment_revoke"),
]