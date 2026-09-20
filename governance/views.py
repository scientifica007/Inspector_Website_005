"""Governance views: guides, assignments and their guards.

All of these are administrator-only, except the two inspector-facing actions
(apply a guide, list your issued assignments).
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from accounts.permissions import admin_required, is_admin
from catalog.models import Reference
from visits.models import Visit
from visits.services import _assert_draft

from . import services
from .constraints import effective_constraints
from .forms import (
    AssignmentCreateForm,
    AssignmentEntryForm,
    GuideForm,
    GuideTargetForm,
    RevokeForm,
)
from .models import (
    Assignment,
    AssignmentStatus,
    Guide,
    GuideStatus,
)


# ---------------------------------------------------------------------------
# Guides
# ---------------------------------------------------------------------------
@login_required
@admin_required
def guide_list(request):
    guides = Guide.objects.select_related("reference").prefetch_related("targets")
    reference_id = request.GET.get("reference")
    if reference_id:
        guides = guides.filter(reference_id=reference_id)
    return render(
        request,
        "governance/guide_list.html",
        {
            "guides": guides,
            "references": Reference.objects.shared().filter(is_active=True),
            "reference_id": reference_id or "",
        },
    )


@login_required
@admin_required
@require_http_methods(["GET", "POST"])
def guide_create(request):
    reference_id = request.GET.get("reference")
    if request.method == "POST" and not reference_id:
        reference_id = request.POST.get("reference")
    reference = None
    if reference_id:
        reference = get_object_or_404(Reference, pk=reference_id)
    form = GuideForm(request.POST or None)
    if request.method == "POST":
        if reference is None:
            messages.error(request, "يجب اختيار مرجع مشترك للدليل.")
        elif form.is_valid():
            guide = services.create_guide(
                actor=request.user,
                reference=reference,
                title=form.cleaned_data["title"],
                description=form.cleaned_data["description"],
            )
            guide.status = form.cleaned_data["status"]
            guide.save(update_fields=["status", "updated_at"])
            messages.success(request, "تم إنشاء الدليل. أضف العناصر المقترحة.")
            return redirect("governance:guide_detail", pk=guide.pk)
    return render(
        request,
        "governance/guide_form.html",
        {
            "form": form,
            "reference": reference,
            "references": Reference.objects.shared().filter(is_active=True),
        },
    )


@login_required
@admin_required
def guide_detail(request, pk):
    guide = get_object_or_404(
        Guide.objects.select_related("reference").prefetch_related("targets"), pk=pk
    )
    return render(
        request,
        "governance/guide_detail.html",
        {"guide": guide, "form": GuideTargetForm()},
    )


@login_required
@admin_required
@require_http_methods(["GET", "POST"])
def guide_edit(request, pk):
    guide = get_object_or_404(Guide, pk=pk)
    form = GuideForm(request.POST or None, instance=guide)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(
            request,
            "تم تحديث الدليل. الزيارات التي طبّقته سابقًا لا تتغير.",
        )
        return redirect("governance:guide_detail", pk=pk)
    return render(
        request, "governance/guide_form.html", {"form": form, "reference": guide.reference}
    )


@login_required
@admin_required
@require_POST
def guide_target_add(request, pk):
    guide = get_object_or_404(Guide, pk=pk)
    form = GuideTargetForm(request.POST)
    if form.is_valid():
        try:
            services.add_guide_target(guide=guide, code=form.cleaned_data["code"])
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            messages.success(request, "تمت إضافة العنصر المقترح.")
    return redirect("governance:guide_detail", pk=pk)


@login_required
@admin_required
@require_POST
def guide_target_remove(request, pk, target_pk):
    guide = get_object_or_404(Guide, pk=pk)
    target = get_object_or_404(guide.targets, pk=target_pk)
    target.delete()
    messages.success(request, "تمت إزالة العنصر من الدليل.")
    return redirect("governance:guide_detail", pk=pk)


@login_required
@admin_required
@require_POST
def guide_delete(request, pk):
    guide = get_object_or_404(Guide, pk=pk)
    guide.delete()
    messages.success(
        request,
        "تم حذف الدليل. الزيارات التي طبّقته تحتفظ بأثر التطبيق وسجلّه.",
    )
    return redirect("governance:guide_list")


@login_required
@admin_required
@require_POST
def guide_publish(request, pk):
    guide = get_object_or_404(Guide, pk=pk)
    guide.status = GuideStatus.PUBLISHED
    guide.save(update_fields=["status", "updated_at"])
    messages.success(request, "أصبح الدليل متاحًا للتطبيق.")
    return redirect("governance:guide_detail", pk=pk)


# ---------------------------------------------------------------------------
# Guided application (inspector)
# ---------------------------------------------------------------------------
@login_required
@require_POST
def guide_apply(request, pk, guide_pk):
    visit = get_object_or_404(Visit, pk=pk)
    if visit.inspector_id != request.user.id:
        raise PermissionDenied("التطبيق متاح لصاحب الزيارة.")
    guide = get_object_or_404(Guide, pk=guide_pk)
    try:
        outcome = services.apply_guide(visit=visit, guide=guide, actor=request.user)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("visits:scope", pk=pk)

    if outcome.already_applied:
        messages.info(request, "تم تطبيق هذا الدليل مسبقًا؛ لم تُضف عناصر جديدة.")
    else:
        text = f"تم تطبيق الدليل: أُضيف {len(outcome.added)} عنصرًا."
        if outcome.skipped:
            text += (
                f" تم تخطّي {len(outcome.skipped)} عنصرًا غير متوافق مع اللقطة "
                f"({'، '.join(outcome.skipped[:5])})."
            )
        messages.success(request, text)
    return redirect("visits:scope", pk=pk)


# ---------------------------------------------------------------------------
# Assignments
# ---------------------------------------------------------------------------
@login_required
@admin_required
def assignment_list(request):
    assignments = Assignment.objects.select_related(
        "visit", "visit__inspector", "issued_by", "revoked_by"
    )
    status = request.GET.get("status")
    if status in AssignmentStatus.values:
        assignments = assignments.filter(status=status)
    return render(
        request,
        "governance/assignment_list.html",
        {"assignments": assignments, "status": status or "",
         "statuses": AssignmentStatus.choices},
    )


@login_required
@admin_required
@require_http_methods(["GET", "POST"])
def assignment_create(request, visit_pk):
    visit = get_object_or_404(Visit, pk=visit_pk)
    try:
        _assert_draft(visit)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("visits:detail", pk=visit_pk)

    form = AssignmentCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        assignment = services.create_assignment(
            visit=visit,
            actor=request.user,
            title=form.cleaned_data["title"],
            instruction=form.cleaned_data["instruction"],
        )
        messages.success(
            request,
            "تم إنشاء مسودة التكليف. لا يظهر للمفتش حتى الإصدار.",
        )
        return redirect("governance:assignment_detail", pk=assignment.pk)
    return render(
        request,
        "governance/assignment_form.html",
        {"form": form, "visit": visit},
    )


@login_required
@admin_required
def assignment_detail(request, pk):
    assignment = get_object_or_404(
        Assignment.objects.select_related("visit", "visit__inspector"), pk=pk
    )
    constraints = effective_constraints(assignment.visit)
    form = AssignmentEntryForm(visit=assignment.visit)
    return render(
        request,
        "governance/assignment_detail.html",
        {
            "assignment": assignment,
            "entries": assignment.entries.select_related("target_node"),
            "form": form,
            "constraints": constraints,
        },
    )


@login_required
@admin_required
@require_POST
def assignment_entry_add(request, pk):
    assignment = get_object_or_404(Assignment, pk=pk)
    form = AssignmentEntryForm(request.POST, visit=assignment.visit)
    if form.is_valid():
        try:
            services.add_assignment_entry(
                assignment=assignment,
                target_node=form.cleaned_data["target_node"],
                scope_locked=form.cleaned_data["scope_locked"],
                completion_required=form.cleaned_data["completion_required"],
                note=form.cleaned_data["note"],
            )
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            messages.success(request, "تمت إضافة البند إلى مسودة التكليف.")
    else:
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)
    return redirect("governance:assignment_detail", pk=pk)


@login_required
@admin_required
@require_POST
def assignment_entry_remove(request, pk, entry_pk):
    assignment = get_object_or_404(Assignment, pk=pk)
    if assignment.status != AssignmentStatus.DRAFT:
        messages.error(request, "لا يمكن تعديل تكليف صادر أو ملغى.")
        return redirect("governance:assignment_detail", pk=pk)
    entry = get_object_or_404(assignment.entries, pk=entry_pk)
    entry.delete()
    messages.success(request, "تمت إزالة البند.")
    return redirect("governance:assignment_detail", pk=pk)


@login_required
@admin_required
@require_POST
def assignment_issue(request, pk):
    assignment = get_object_or_404(Assignment, pk=pk)
    try:
        services.issue_assignment(assignment=assignment, actor=request.user)
    except ValidationError as exc:
        messages.error(
            request,
            "لم يصدر أي جزء من التكليف: " + "; ".join(exc.messages),
        )
        return redirect("governance:assignment_detail", pk=pk)
    messages.success(
        request,
        "تم إصدار التكليف. أصبح سجلًا رسميًا ظاهرًا للمفتش.",
    )
    return redirect("governance:assignment_detail", pk=pk)


@login_required
@admin_required
@require_http_methods(["GET", "POST"])
def assignment_revoke(request, pk):
    assignment = get_object_or_404(Assignment, pk=pk)
    form = RevokeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.revoke_assignment(
                assignment=assignment,
                actor=request.user,
                reason=form.cleaned_data["reason"],
            )
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return redirect("governance:assignment_detail", pk=pk)
        messages.success(
            request,
            "تم إلغاء التكليف وتسجيل السبب. القيود الأساسية وقيود التكليفات الأخرى تبقى سارية.",
        )
        return redirect("governance:assignment_detail", pk=pk)
    return render(
        request,
        "governance/assignment_revoke.html",
        {"form": form, "assignment": assignment},
    )


@login_required
def my_assignments(request):
    if is_admin(request.user):
        return redirect("governance:assignment_list")
    assignments = Assignment.objects.filter(
        visit__inspector=request.user, status=AssignmentStatus.ISSUED
    ).select_related("visit", "visit__institution")
    return render(
        request,
        "governance/my_assignments.html",
        {"assignments": assignments},
    )