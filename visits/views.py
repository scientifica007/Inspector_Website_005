"""Visit views.

Permission model enforced here on the server:

* an inspector sees and acts on their own visits;
* an administrator may read any visit (needed to issue assignments and audit),
  but never edits execution data, and cannot delete another inspector's draft;
* completed visits are read-only for everyone.
"""

from __future__ import annotations

import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.permissions import is_admin
from catalog.models import NodeType, Reference
from governance.constraints import effective_constraints
from governance.models import Assignment, AssignmentStatus, Guide, GuideStatus

from . import services
from .export import build_visit_export
from .forms import ItemResultForm, LocalNodeForm, ValueForm, VisitCreateForm, VisitNotesForm
from .models import ItemResult, NodeStatus, Origin, Visit, VisitNode, VisitStatus


# ---------------------------------------------------------------------------
# Access helpers
# ---------------------------------------------------------------------------
def _get_visit(request, pk: int) -> Visit:
    visit = get_object_or_404(
        Visit.objects.select_related("institution", "inspector", "source_reference"), pk=pk
    )
    can_read = visit.inspector_id == request.user.id or is_admin(request.user)
    if not can_read:
        raise PermissionDenied("لا تملك صلاحية الوصول إلى هذه الزيارة.")
    return visit


def _assert_owner_editable(request, visit: Visit) -> None:
    if visit.inspector_id != request.user.id:
        raise PermissionDenied("التنفيذ مسؤولية المفتش صاحب الزيارة.")
    if not visit.is_draft:
        raise PermissionDenied("الزيارة مكتملة: الحقيقة التاريخية محمية.")


def _tree_rows(visit: Visit, nodes: list[VisitNode], constraints=None) -> list[dict]:
    """Build display rows with depth, for an indented tree.

    When ``constraints`` is supplied each row also carries the resolved
    scope-lock and completion-requirement flags for that node.
    """
    by_id = {node.id: node for node in nodes}
    depths: dict[int, int] = {}

    def depth_of(node: VisitNode) -> int:
        if node.id in depths:
            return depths[node.id]
        if node.parent_id is None or node.parent_id not in by_id:
            depths[node.id] = 0
        else:
            depths[node.id] = depth_of(by_id[node.parent_id]) + 1
        return depths[node.id]

    rows = []
    for node in nodes:
        rows.append(
            {
                "node": node,
                "depth": depth_of(node),
                "scope_locked": bool(constraints and constraints.is_scope_locked(node)),
                "completion_required": bool(
                    constraints and constraints.is_completion_required(node)
                ),
            }
        )
    return rows


def _ordered_nodes(visit: Visit) -> list[VisitNode]:
    nodes = list(visit.nodes.select_related("parent").order_by("position", "id"))
    by_parent: dict[int | None, list[VisitNode]] = {}
    for node in nodes:
        by_parent.setdefault(node.parent_id, []).append(node)
    ordered: list[VisitNode] = []

    def walk(parent_id) -> None:
        for node in by_parent.get(parent_id, []):
            ordered.append(node)
            walk(node.id)

    walk(None)
    return ordered


# ---------------------------------------------------------------------------
# List / create
# ---------------------------------------------------------------------------
@login_required
def visit_list(request):
    queryset = Visit.objects.select_related("institution", "inspector")
    if not is_admin(request.user):
        queryset = queryset.filter(inspector=request.user)
    status = request.GET.get("status", "")
    if status in VisitStatus.values:
        queryset = queryset.filter(status=status)
    return render(
        request,
        "visits/list.html",
        {"visits": queryset, "status": status, "statuses": VisitStatus.choices},
    )


@login_required
def visit_create(request):
    if is_admin(request.user):
        raise PermissionDenied("إنشاء الزيارات متاح للمفتشين. المدير يشرف ولا ينفذ.")
    form = VisitCreateForm(request.POST or None, user=request.user)
    if request.method == "POST" and form.is_valid():
        visit = services.create_visit(
            actor=request.user,
            institution=form.cleaned_data["institution"],
            visit_date=form.cleaned_data["visit_date"],
            title=form.cleaned_data["title"],
            reference=form.cleaned_data["reference"],
            notes=form.cleaned_data["notes"],
        )
        messages.success(
            request,
            "تم إنشاء الزيارة. النطاق فارغ في البداية — أضف ما تريد تفتيشه.",
        )
        return redirect("visits:scope", pk=visit.pk)
    return render(request, "visits/create.html", {"form": form})


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@login_required
def visit_detail(request, pk):
    visit = _get_visit(request, pk)
    constraints = effective_constraints(visit)
    progress = services.compute_progress(visit)
    outstanding = services.outstanding_requirements(visit)
    is_owner = visit.inspector_id == request.user.id

    issued_assignments = visit.assignments.filter(
        status=AssignmentStatus.ISSUED
    ).prefetch_related("entries__target_node")
    draft_assignments = (
        visit.assignments.filter(status=AssignmentStatus.DRAFT).prefetch_related("entries")
        if is_admin(request.user)
        else Assignment.objects.none()
    )

    context = {
        "visit": visit,
        "progress": progress,
        "outstanding": outstanding,
        "constraints": constraints,
        "is_owner": is_owner,
        "can_edit": is_owner and visit.is_draft,
        "issued_assignments": issued_assignments,
        "draft_assignments": draft_assignments,
        "guide_applications": visit.guide_applications.select_related("guide"),
        "revoked_assignments": visit.assignments.filter(
            status=AssignmentStatus.REVOKED
        ).select_related("revoked_by"),
    }
    return render(request, "visits/detail.html", context)


@login_required
def visit_edit(request, pk):
    visit = _get_visit(request, pk)
    _assert_owner_editable(request, visit)
    form = VisitNotesForm(request.POST or None, instance=visit)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "تم تحديث بيانات الزيارة.")
        return redirect("visits:detail", pk=visit.pk)
    return render(request, "visits/edit.html", {"form": form, "visit": visit})


@login_required
@require_POST
def visit_delete(request, pk):
    visit = _get_visit(request, pk)
    if visit.inspector_id != request.user.id:
        raise PermissionDenied(
            "لا يمكن حذف مسودة مفتش آخر. المسودة ملك لصاحبها."
        )
    try:
        services.delete_draft(visit=visit, actor=request.user)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("visits:detail", pk=pk)
    messages.success(request, "تم حذف المسودة.")
    return redirect("visits:list")


# ---------------------------------------------------------------------------
# Scope builder
# ---------------------------------------------------------------------------
@login_required
def visit_scope(request, pk):
    visit = _get_visit(request, pk)
    _assert_owner_editable(request, visit)
    nodes = _ordered_nodes(visit)
    constraints = effective_constraints(visit)
    rows = _tree_rows(visit, nodes, constraints)
    available_guides = Guide.objects.filter(
        reference=visit.source_reference, status=GuideStatus.PUBLISHED
    ) if visit.source_reference_id else Guide.objects.none()
    applied_guide_ids = set(
        visit.guide_applications.values_list("guide_id", flat=True)
    )
    progress = services.compute_progress(visit)
    return render(
        request,
        "visits/scope.html",
        {
            "visit": visit,
            "rows": rows,
            "constraints": constraints,
            "available_guides": available_guides,
            "applied_guide_ids": applied_guide_ids,
            "progress": progress,
            "local_form": LocalNodeForm(visit=visit),
            "has_snapshot": visit.source_reference_id is not None
            or visit.nodes.exists(),
        },
    )


@login_required
@require_POST
def scope_select(request, pk, node_pk):
    visit = _get_visit(request, pk)
    _assert_owner_editable(request, visit)
    node = get_object_or_404(VisitNode, pk=node_pk, visit=visit)
    try:
        services.select_node(visit=visit, node=node, origin=Origin.MANUAL)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return redirect("visits:scope", pk=pk)


@login_required
@require_POST
def scope_deselect(request, pk, node_pk):
    visit = _get_visit(request, pk)
    _assert_owner_editable(request, visit)
    node = get_object_or_404(VisitNode, pk=node_pk, visit=visit)
    try:
        services.deselect_node(visit=visit, node=node)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return redirect("visits:scope", pk=pk)


@login_required
@require_POST
def scope_exclude(request, pk, node_pk):
    visit = _get_visit(request, pk)
    _assert_owner_editable(request, visit)
    node = get_object_or_404(VisitNode, pk=node_pk, visit=visit)
    constraints = effective_constraints(visit)
    if constraints.is_scope_locked(node):
        messages.error(request, "لا يمكن استبعاد عنصر عليه قيد نطاق سارٍ.")
        return redirect("visits:scope", pk=pk)
    services.exclude_node(visit=visit, node=node)
    messages.success(
        request, "تم استبعاد العنصر مع الاحتفاظ بكل بياناته. يمكنك استرجاعه لاحقًا."
    )
    return redirect("visits:scope", pk=pk)


@login_required
@require_POST
def scope_restore(request, pk, node_pk):
    visit = _get_visit(request, pk)
    _assert_owner_editable(request, visit)
    node = get_object_or_404(VisitNode, pk=node_pk, visit=visit)
    services.restore_node(visit=visit, node=node)
    messages.success(request, "تم استرجاع العنصر بنفس بياناته السابقة.")
    return redirect("visits:scope", pk=pk)


@login_required
@require_POST
def local_node_create(request, pk):
    visit = _get_visit(request, pk)
    _assert_owner_editable(request, visit)
    form = LocalNodeForm(request.POST, visit=visit)
    if form.is_valid():
        services.add_local_node(
            visit=visit,
            node_type=form.cleaned_data["node_type"],
            title=form.cleaned_data["title"],
            body=form.cleaned_data["body"],
            parent=form.cleaned_data["parent"],
            value_type=form.cleaned_data["value_type"] or "NONE",
        )
        messages.success(request, "تمت إضافة محتوى محلي إلى الزيارة.")
    else:
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)
    return redirect("visits:scope", pk=pk)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
@login_required
def visit_execute(request, pk):
    visit = _get_visit(request, pk)
    _assert_owner_editable(request, visit)
    nodes = _ordered_nodes(visit)
    constraints = effective_constraints(visit)
    progress = services.compute_progress(visit)
    outstanding_ids = {node.id for node in services.outstanding_requirements(visit)}

    # Execution shows every active selected element, plus the active structural
    # ancestors that give a deeply selected element its context. Excluded
    # elements are not shown here; they are restored from the scope screen.
    by_id = {node.id: node for node in nodes}
    selected_active = [
        node for node in nodes if node.is_active and node.is_selected
    ]
    keep: set[int] = {node.id for node in selected_active}
    for node in selected_active:
        parent = by_id.get(node.parent_id) if node.parent_id else None
        while parent is not None and parent.is_active:
            keep.add(parent.id)
            parent = by_id.get(parent.parent_id) if parent.parent_id else None

    visible = [node for node in nodes if node.id in keep]
    rows = _tree_rows(visit, visible, constraints)
    return render(
        request,
        "visits/execute.html",
        {
            "visit": visit,
            "rows": rows,
            "constraints": constraints,
            "progress": progress,
            "outstanding_ids": outstanding_ids,
        },
    )


@login_required
@require_POST
def record_item(request, pk, node_pk):
    visit = _get_visit(request, pk)
    _assert_owner_editable(request, visit)
    node = get_object_or_404(VisitNode, pk=node_pk, visit=visit)
    form = ItemResultForm(request.POST)
    if form.is_valid():
        try:
            services.record_result(
                visit=visit,
                node=node,
                result=form.cleaned_data["result"] or "",
                observation=form.cleaned_data["observation"],
            )
            messages.success(request, f"تم تسجيل نتيجة {node.code}.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
    else:
        messages.error(request, "تعذّر تسجيل النتيجة.")
    return redirect("visits:execute", pk=pk)


@login_required
@require_POST
def record_value(request, pk, node_pk):
    visit = _get_visit(request, pk)
    _assert_owner_editable(request, visit)
    node = get_object_or_404(VisitNode, pk=node_pk, visit=visit)
    form = ValueForm(request.POST)
    if form.is_valid():
        try:
            services.record_value(
                visit=visit,
                node=node,
                value_text=form.cleaned_data["value_text"],
                value_number=form.cleaned_data["value_number"],
                value_date=form.cleaned_data["value_date"],
                value_boolean=form.cleaned_data["value_boolean"],
                observation=form.cleaned_data["observation"],
            )
            messages.success(request, f"تم تسجيل قيمة {node.code}.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
    else:
        messages.error(request, "تعذّر تسجيل القيمة.")
    return redirect("visits:execute", pk=pk)


@login_required
@require_POST
def complete_visit_view(request, pk):
    visit = _get_visit(request, pk)
    _assert_owner_editable(request, visit)
    try:
        services.complete_visit(visit=visit, actor=request.user)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("visits:execute", pk=pk)
    messages.success(request, "تم إكمال الزيارة وأصبحت سجلًا تاريخيًا محميًا.")
    return redirect("visits:detail", pk=pk)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
@login_required
def visit_export(request, pk):
    visit = _get_visit(request, pk)
    payload = build_visit_export(visit)
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    if request.GET.get("download") == "1":
        response = HttpResponse(body, content_type="application/json; charset=utf-8")
        response["Content-Disposition"] = (
            f'attachment; filename="visit-{visit.pk}-export-v{payload["version"]}.json"'
        )
        return response
    return HttpResponse(body, content_type="application/json; charset=utf-8")