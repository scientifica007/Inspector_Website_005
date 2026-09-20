from django.conf import settings


def navigation(request):
    """Expose coarse navigation flags so templates do not re-derive roles.

    These flags only shape what is *shown*. Every view enforces its own
    permissions on the server; hiding a link is never a security boundary.
    """
    from accounts.models import Role

    user = getattr(request, "user", None)
    authenticated = bool(user and user.is_authenticated)
    return {
        "nav_is_authenticated": authenticated,
        "nav_is_admin": authenticated and user.role == Role.ADMIN,
        "nav_is_inspector": authenticated and user.role == Role.INSPECTOR,
        "app_version": settings.APP_VERSION,
    }