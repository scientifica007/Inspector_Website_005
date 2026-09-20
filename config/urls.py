from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("core.urls")),
    path("accounts/", include("accounts.urls")),
    path("institutions/", include("institutions.urls")),
    path("references/", include("catalog.urls")),
    path("visits/", include("visits.urls")),
    path("governance/", include("governance.urls")),
]
