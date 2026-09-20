"""Reusable permission helpers.

Server-side enforcement is mandatory; templates only decide what to render.
"""

from functools import wraps

from django.core.exceptions import PermissionDenied

from .models import Role


def is_admin(user) -> bool:
    return bool(user and user.is_authenticated and user.role == Role.ADMIN)


def is_inspector(user) -> bool:
    return bool(user and user.is_authenticated and user.role == Role.INSPECTOR)


def admin_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not is_admin(request.user):
            raise PermissionDenied("هذه العملية متاحة لمدير النظام فقط.")
        return view_func(request, *args, **kwargs)

    return wrapper


def inspector_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not is_inspector(request.user):
            raise PermissionDenied("هذه العملية متاحة للمفتشين فقط.")
        return view_func(request, *args, **kwargs)

    return wrapper