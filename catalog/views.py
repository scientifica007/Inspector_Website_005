from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from accounts.permissions import admin_required, is_admin

from . import services
from .forms import ReferenceForm, ReferenceNodeForm, ReviewForm, SubmissionForm
from .models import (
    Reference,
    ReferenceNode,
    ReferenceSubmission,
    ReferenceVisibility,
    SubmissionStatus,
)


def _visible_or_403(request, pk: int) -> Reference:
    reference = get_object_or_404(Reference, pk=pk)
    if reference.is_shared:
        if reference.is_deleted and not is_admin(request.user):
            raise PermissionDenied("هذا المرجع محذوف.")
        return reference
    if reference.owner_id != request.user.id and not is_admin(request.user):
        raise PermissionDenied("هذا مرجع خاص بمفتش آخر.")
    return reference


def _assert_can_edit(request, reference: Reference) -> None:
    if reference.is_shared:
        if not is_admin(request.user):
            raise PermissionDenied("تعديل المراجع المشتركة متاح لمدير النظام فقط.")
        return
    if reference.owner_id != request.user.id:
        raise PermissionDenied("لا تملك هذا المرجع الخاص.")


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------
@login_required
def reference_list(request):
    base = Reference.objects.visible_to(request.user)
    if is_admin(request.user):
        shared = base.filter(visibility=ReferenceVisibility.SHARED)
        private = base.filter(visibility=ReferenceVisibility.PRIVATE)
    else:
        shared = base.filter(visibility=ReferenceVisibility.SHARED, deleted_at__isnull=True)
        private = base.filter(
            visibility=ReferenceVisibility.PRIVATE, owner=request.user
        )
    return render(
        request,
        "catalog/list.html",
        {"shared_references": shared, "private_references": private},
    )


@login_required
def reference_detail(request, pk):
    reference = _visible_or_403(request, pk)
    nodes = services.ordered_tree(reference)
    pending_submission = reference.submissions.filter(
        status=SubmissionStatus.PENDING
    ).first()
    return render(
        request,
        "catalog/detail.html",
        {
            "reference": reference,
            "nodes": nodes,
            "can_edit": _can_edit(request, reference),
            "pending_submission": pending_submission,
            "submissions": reference.submissions.select_related("reviewed_by")[:10],
        },
    )


def _can_edit(request, reference: Reference) -> bool:
    if reference.is_shared:
        return is_admin(request.user)
    return reference.owner_id == request.user.id


@login_required
@require_http_methods(["GET", "POST"])
def reference_create_shared(request):
    if not is_admin(request.user):
        raise PermissionDenied("إنشاء المراجع المشتركة متاح لمدير النظام فقط.")
    form = ReferenceForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        reference = services.create_reference(
            actor=request.user,
            title=form.cleaned_data["title"],
            description=form.cleaned_data["description"],
            visibility=ReferenceVisibility.SHARED,
        )
        messages.success(request, "تم إنشاء المرجع المشترك.")
        return redirect("catalog:detail", pk=reference.pk)
    return render(
        request,
        "catalog/form.html",
        {"form": form, "title": "مرجع مشترك جديد"},
    )


@login_required
@require_http_methods(["GET", "POST"])
def reference_create_private(request):
    form = ReferenceForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        reference = services.create_reference(
            actor=request.user,
            title=form.cleaned_data["title"],
            description=form.cleaned_data["description"],
            visibility=ReferenceVisibility.PRIVATE,
            owner=request.user,
        )
        messages.success(request, "تم إنشاء مرجعك الخاص.")
        return redirect("catalog:detail", pk=reference.pk)
    return render(
        request,
        "catalog/form.html",
        {"form": form, "title": "مرجع خاص جديد"},
    )


@login_required
@require_http_methods(["GET", "POST"])
def reference_edit(request, pk):
    reference = get_object_or_404(Reference, pk=pk)
    _assert_can_edit(request, reference)
    form = ReferenceForm(request.POST or None, instance=reference)
    if request.method == "POST" and form.is_valid():
        form.save()
        services.touch_revision(reference)
        messages.success(request, "تم تحديث المرجع.")
        return redirect("catalog:detail", pk=reference.pk)
    return render(
        request,
        "catalog/form.html",
        {"form": form, "title": "تعديل المرجع"},
    )


@login_required
@require_POST
def reference_delete(request, pk):
    reference = get_object_or_404(Reference, pk=pk)
    _assert_can_edit(request, reference)
    services.soft_delete_reference(reference)
    messages.success(
        request,
        "تم حذف المرجع من المكتبة الحية. الزيارات السابقة تحتفظ بلقطاتها المجمّدة.",
    )
    return redirect("catalog:list")


@login_required
@require_POST
def reference_restore(request, pk):
    reference = get_object_or_404(Reference, pk=pk)
    if not is_admin(request.user):
        raise PermissionDenied("استرجاع المراجع المحذوفة متاح لمدير النظام فقط.")
    services.restore_reference(reference)
    messages.success(request, "تم استرجاع المرجع.")
    return redirect("catalog:detail", pk=reference.pk)


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------
@login_required
@require_http_methods(["GET", "POST"])
def node_create(request, pk):
    reference = get_object_or_404(Reference, pk=pk)
    _assert_can_edit(request, reference)
    initial = {}
    parent_id = request.GET.get("parent")
    if parent_id:
        initial["parent"] = parent_id
    form = ReferenceNodeForm(request.POST or None, reference=reference, initial=initial)
    if request.method == "POST" and form.is_valid():
        node = form.save()
        services.touch_revision(reference)
        messages.success(request, "تمت إضافة العنصر.")
        return redirect("catalog:detail", pk=reference.pk)
    return render(
        request,
        "catalog/node_form.html",
        {"form": form, "reference": reference, "title": "عنصر جديد"},
    )


@login_required
@require_http_methods(["GET", "POST"])
def node_edit(request, pk, node_pk):
    reference = get_object_or_404(Reference, pk=pk)
    _assert_can_edit(request, reference)
    node = get_object_or_404(ReferenceNode, pk=node_pk, reference=reference)
    form = ReferenceNodeForm(request.POST or None, instance=node, reference=reference)
    if request.method == "POST" and form.is_valid():
        form.save()
        services.touch_revision(reference)
        messages.success(request, "تم تحديث العنصر.")
        return redirect("catalog:detail", pk=reference.pk)
    return render(
        request,
        "catalog/node_form.html",
        {"form": form, "reference": reference, "node": node, "title": "تعديل عنصر"},
    )


@login_required
@require_POST
def node_delete(request, pk, node_pk):
    reference = get_object_or_404(Reference, pk=pk)
    _assert_can_edit(request, reference)
    node = get_object_or_404(ReferenceNode, pk=node_pk, reference=reference)
    node.delete()
    services.touch_revision(reference)
    messages.success(
        request,
        "تم حذف العنصر من المرجع الحي. الزيارات المنشأة سابقًا لا تتأثر.",
    )
    return redirect("catalog:detail", pk=reference.pk)


# ---------------------------------------------------------------------------
# Private reference submissions
# ---------------------------------------------------------------------------
@login_required
@require_http_methods(["GET", "POST"])
def submission_create(request, pk):
    reference = get_object_or_404(Reference, pk=pk)
    if reference.owner_id != request.user.id:
        raise PermissionDenied("يمكنك إرسال مراجعك الخاصة فقط.")
    form = SubmissionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.create_submission(
                reference=reference,
                actor=request.user,
                note=form.cleaned_data["note"],
            )
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            messages.success(
                request,
                "تم إرسال المقترح مع لقطة مجمّدة. تعديلك لاحقًا لن يغيّر ما يراجعه المدير.",
            )
            return redirect("catalog:detail", pk=reference.pk)
    return render(
        request,
        "catalog/submission_form.html",
        {"form": form, "reference": reference},
    )


@login_required
@admin_required
def submission_list(request):
    submissions = ReferenceSubmission.objects.select_related(
        "reference", "submitted_by", "reviewed_by"
    )
    status = request.GET.get("status")
    if status in SubmissionStatus.values:
        submissions = submissions.filter(status=status)
    return render(
        request,
        "catalog/submission_list.html",
        {"submissions": submissions, "status": status or "",
         "statuses": SubmissionStatus.choices},
    )


@login_required
@admin_required
def submission_detail(request, pk):
    submission = get_object_or_404(
        ReferenceSubmission.objects.select_related("reference", "submitted_by"), pk=pk
    )
    return render(
        request,
        "catalog/submission_detail.html",
        {"submission": submission, "form": ReviewForm()},
    )


@login_required
@admin_required
@require_POST
def submission_approve(request, pk):
    submission = get_object_or_404(ReferenceSubmission, pk=pk)
    form = ReviewForm(request.POST)
    form.is_valid()
    try:
        shared = services.approve_submission(
            submission=submission,
            actor=request.user,
            note=form.cleaned_data.get("note", ""),
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("catalog:submission_detail", pk=pk)
    messages.success(
        request,
        "تم إنشاء مرجع مشترك مستقل من اللقطة. المرجع الخاص يبقى ملكًا لصاحبه.",
    )
    return redirect("catalog:detail", pk=shared.pk)


@login_required
@admin_required
@require_POST
def submission_reject(request, pk):
    submission = get_object_or_404(ReferenceSubmission, pk=pk)
    form = ReviewForm(request.POST)
    form.is_valid()
    try:
        services.reject_submission(
            submission=submission,
            actor=request.user,
            note=form.cleaned_data.get("note", ""),
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("catalog:submission_detail", pk=pk)
    messages.success(request, "تم رفض المقترح. المرجع الأصلي يبقى خاصًا بمفتشه.")
    return redirect("catalog:submission_list")