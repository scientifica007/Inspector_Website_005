from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from accounts.models import Role


@login_required
def home(request):
    return render(
        request,
        "core/home.html",
        {"is_admin": request.user.role == Role.ADMIN},
    )