from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views.decorators.http import require_http_methods

from .forms import LoginForm, RegisterForm, UserForm
from .models import Role, User
from .permissions import admin_required


class AppLoginView(auth_views.LoginView):
    form_class = LoginForm
    template_name = "accounts/login.html"


class AppLogoutView(auth_views.LogoutView):
    next_page = reverse_lazy("accounts:login")


@require_http_methods(["GET", "POST"])
def register(request):
    if request.user.is_authenticated:
        return redirect("core:home")
    form = RegisterForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        user.role = Role.INSPECTOR
        user.save(update_fields=["role"])
        return redirect("accounts:login")
    return render(request, "accounts/register.html", {"form": form})


@login_required
def profile(request):
    return render(request, "accounts/profile.html")


# ---------------------------------------------------------------------------
# User administration
# ---------------------------------------------------------------------------
@login_required
@admin_required
def user_list(request):
    users = User.objects.all().order_by("username")
    return render(request, "accounts/user_list.html", {"users": users})


@login_required
@admin_required
@require_http_methods(["GET", "POST"])
def user_create(request):
    form = UserForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        messages.success(request, "تم إنشاء المستخدم.")
        return redirect("accounts:user_list")
    return render(
        request, "accounts/user_form.html", {"form": form, "title": "مستخدم جديد"}
    )


@login_required
@admin_required
@require_http_methods(["GET", "POST"])
def user_edit(request, pk):
    user = get_object_or_404(User, pk=pk)
    form = UserForm(request.POST or None, instance=user)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "تم تحديث المستخدم.")
        return redirect("accounts:user_list")
    return render(
        request,
        "accounts/user_form.html",
        {"form": form, "title": "تعديل مستخدم", "object": user},
    )


@login_required
@admin_required
@require_http_methods(["POST"])
def user_toggle_active(request, pk):
    """Deactivating replaces deletion: an administrator is never removed.

    An administrator cannot lock their own account, which prevents a single
    mistake from removing all administrative access.
    """
    user = get_object_or_404(User, pk=pk)
    if user.pk == request.user.pk:
        messages.error(request, "لا يمكنك تعطيل حسابك الخاص.")
        return redirect("accounts:user_list")
    user.is_active = not user.is_active
    user.save(update_fields=["is_active"])
    messages.success(
        request, "تم تفعيل المستخدم." if user.is_active else "تم تعطيل المستخدم."
    )
    return redirect("accounts:user_list")