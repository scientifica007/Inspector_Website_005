from django.urls import path

from . import views

app_name = "institutions"

urlpatterns = [
    path("", views.institution_list, name="list"),
    path("new/", views.institution_create, name="create"),
    path("<int:pk>/", views.institution_detail, name="detail"),
    path("<int:pk>/edit/", views.institution_edit, name="edit"),
]