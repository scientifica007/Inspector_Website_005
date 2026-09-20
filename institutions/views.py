from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from accounts.permissions import admin_required, is_admin

from .forms import InstitutionForm
from .models import Institution


@login_required
def institution_list(request):
    query = request.GET.get("q", "").strip()
    queryset = Institution.objects.all()
    if query:
        queryset = queryset.filter(
            Q(name__icontains=query) | Q(code__icontains=query) | Q(location__icontains=query)
        )
    if not is_admin(request.user):
        queryset = queryset.filter(is_active=True)
    return render(
        request,
        "institutions/list.html",
        {"institutions": queryset, "query": query},
    )


@login_required
@admin_required
@require_http_methods(["GET", "POST"])
def institution_create(request):
    form = InstitutionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        institution = form.save()
        messages.success(request, "تم إنشاء المؤسسة.")
        return redirect("institutions:detail", pk=institution.pk)
    return render(
        request, "institutions/form.html", {"form": form, "title": "مؤسسة جديدة"}
    )


@login_required
def institution_detail(request, pk):
    institution = get_object_or_404(Institution, pk=pk)
    if not is_admin(request.user) and not institution.is_active:
        messages.error(request, "هذه المؤسسة غير نشطة.")
        return redirect("institutions:list")
    return render(request, "institutions/detail.html", {"institution": institution})


@login_required
@admin_required
@require_http_methods(["GET", "POST"])
def institution_edit(request, pk):
    institution = get_object_or_404(Institution, pk=pk)
    form = InstitutionForm(request.POST or None, instance=institution)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "تم تحديث المؤسسة.")
        return redirect("institutions:detail", pk=institution.pk)
    return render(
        request,
        "institutions/form.html",
        {"form": form, "title": "تعديل مؤسسة", "institution": institution},
    )