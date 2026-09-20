"""Guide and Assignment services.

Guides are suggestions. Assignments are orders. Keeping them in one module makes
the contrast explicit: they share almost nothing except that both reference
stable node codes inside a frozen snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from visits.models import NodeStatus, Origin, Visit, VisitNode
from visits.services import _assert_draft, nodes_by_code

from .constraints import effective_constraints
from .models import (
    Assignment,
    AssignmentEntry,
    AssignmentStatus,
    Guide,
    GuideApplication,
    GuideStatus,
    GuideTarget,
)


# ---------------------------------------------------------------------------
# Guides
# ---------------------------------------------------------------------------
@transaction.atomic
def create_guide(*, actor, reference, title: str, description: str = "") -> Guide:
    if not reference.is_shared:
        raise ValidationError("الدليل يجب أن يرتبط بمرجع مشترك.")
    guide = Guide.objects.create(
        title=title,
        description=description,
        reference=reference,
        created_by=actor,
        status=GuideStatus.DRAFT,
    )
    return guide


@transaction.atomic
def add_guide_target(
    *, guide: Guide, code: str, title_snapshot: str = "", node_type_snapshot: str = ""
) -> GuideTarget:
    """Attach a suggestion by stable code.

    The target is validated against the *live* reference: a guide may only
    propose elements the reference actually contains. Whether an individual
    visit can honour it is decided at application time.
    """
    node = guide.reference.nodes.filter(code=code).first()
    if node is None:
        raise ValidationError(
            f"المعرّف {code} غير موجود في المرجع {guide.reference.title}."
        )
    if GuideTarget.objects.filter(guide=guide, code=code).exists():
        raise ValidationError("هذا العنصر مقترح في الدليل بالفعل.")
    position = (
        GuideTarget.objects.filter(guide=guide)
        .order_by("-position")
        .values_list("position", flat=True)
        .first()
        or 0
    )
    return GuideTarget.objects.create(
        guide=guide,
        code=code,
        title_snapshot=title_snapshot or node.title,
        node_type_snapshot=node_type_snapshot or node.node_type,
        position=position + 10,
    )


@dataclass(frozen=True)
class GuideApplicationOutcome:
    added: list[str]
    skipped: list[str]
    already_applied: bool = False


@transaction.atomic
def apply_guide(*, visit: Visit, guide: Guide, actor) -> GuideApplicationOutcome:
    """Apply a guide's suggestions to a draft visit.

    Guarantees:

    * idempotent — applying the same guide twice adds nothing the second time;
    * additive only — never removes or relocks anything;
    * origin-preserving — elements that were already in scope keep their origin;
    * tolerant of drift — codes missing from the frozen snapshot are skipped and
      reported, and the live reference is *never* queried for a replacement.
    """
    _assert_draft(visit)
    if guide.status != GuideStatus.PUBLISHED:
        raise ValidationError("لا يمكن تطبيق دليل غير منشور.")
    if visit.source_reference_id != guide.reference_id:
        raise ValidationError(
            "لا يمكن تطبيق هذا الدليل: الزيارة لم تُنشأ من المرجع المرتبط بالدليل."
        )

    existing_application = GuideApplication.objects.filter(
        visit=visit, guide=guide
    ).first()

    by_code = nodes_by_code(visit)
    added: list[str] = []
    skipped: list[str] = []

    for target in guide.targets.order_by("position", "id"):
        node = by_code.get(target.code)
        if node is None:
            # Temporal mismatch: the frozen snapshot predates this suggestion.
            skipped.append(target.code)
            continue
        if node.is_selected and not node.is_excluded:
            skipped.append(target.code)
            continue
        _apply_target(visit, node, by_code, added)

    if existing_application is None:
        GuideApplication.objects.create(
            visit=visit,
            guide=guide,
            guide_title_snapshot=guide.title,
            guide_version_snapshot=guide.version,
            applied_by=actor,
            added_count=len(added),
            skipped_count=len(skipped),
            skipped_codes=skipped,
        )
        already_applied = False
    else:
        already_applied = not added

    return GuideApplicationOutcome(
        added=added, skipped=skipped, already_applied=already_applied
    )


def _apply_target(
    visit: Visit, node: VisitNode, by_code: dict[str, VisitNode], added: list[str]
) -> None:
    """Add a target and its structural ancestors as context, never as constraints."""
    # Structural ancestors must exist in the snapshot for the target to be
    # reachable; they are rendered as context only.
    fields: dict = {"is_selected": True, "status": NodeStatus.ACTIVE,
                    "excluded_at": None, "excluded_via": None}
    if node.origin is None:
        fields["origin"] = Origin.GUIDE
    VisitNode.objects.filter(pk=node.pk).update(**fields)
    node.is_selected = True
    node.status = NodeStatus.ACTIVE
    if node.origin is None:
        node.origin = Origin.GUIDE

    if node.is_branch:
        for child in _branch_descendants(visit, node):
            child_fields: dict = {
                "is_selected": True,
                "status": NodeStatus.ACTIVE,
                "excluded_at": None,
                "excluded_via": None,
            }
            if child.origin is None:
                child_fields["origin"] = Origin.GUIDE
                child.origin = Origin.GUIDE
            VisitNode.objects.filter(pk=child.pk).update(**child_fields)
            child.is_selected = True
            child.status = NodeStatus.ACTIVE
    added.append(node.code)


def _branch_descendants(visit: Visit, node: VisitNode) -> list[VisitNode]:
    result: list[VisitNode] = []
    frontier = [node]
    while frontier:
        children = list(
            VisitNode.objects.filter(visit=visit, parent__in=frontier).order_by(
                "position", "id"
            )
        )
        result.extend(children)
        frontier = children
    return result


# ---------------------------------------------------------------------------
# Assignments
# ---------------------------------------------------------------------------
@transaction.atomic
def create_assignment(
    *, visit: Visit, actor, title: str, instruction: str = ""
) -> Assignment:
    _assert_draft(visit)
    if visit.is_completed:
        raise ValidationError("لا يمكن إنشاء تكليف على زيارة مكتملة.")
    return Assignment.objects.create(
        visit=visit,
        title=title,
        instruction=instruction,
        created_by=actor,
        status=AssignmentStatus.DRAFT,
    )


@transaction.atomic
def add_assignment_entry(
    *,
    assignment: Assignment,
    target_node: VisitNode,
    scope_locked: bool = False,
    completion_required: bool = False,
    note: str = "",
) -> AssignmentEntry:
    if assignment.status != AssignmentStatus.DRAFT:
        raise ValidationError("لا يمكن تعديل بنود تكليف صادر أو ملغى.")
    if target_node.visit_id != assignment.visit_id:
        raise ValidationError("العنصر المستهدف لا ينتمي إلى زيارة التكليف.")
    if not (scope_locked or completion_required):
        raise ValidationError("على بند التكليف أن يفرض قيد نطاق أو متطلب إتمام.")
    if AssignmentEntry.objects.filter(
        assignment=assignment, target_node=target_node
    ).exists():
        raise ValidationError("هذا العنصر مستهدف في التكليف بالفعل.")
    if completion_required and not target_node.is_completable and not target_node.is_branch:
        raise ValidationError("متطلب الإتمام يُطبَّق على عناصر قابلة للإنجاز فقط.")
    return AssignmentEntry.objects.create(
        assignment=assignment,
        target_node=target_node,
        scope_locked=scope_locked,
        completion_required=completion_required,
        note=note,
    )


@transaction.atomic
def issue_assignment(*, assignment: Assignment, actor) -> Assignment:
    """Issue an assignment atomically: all entries valid, or nothing is issued.

    Validation happens before any write. If a single target is unacceptable the
    whole issue is refused and the assignment stays a draft.
    """
    visit = assignment.visit
    _assert_draft(visit)
    if assignment.status != AssignmentStatus.DRAFT:
        raise ValidationError("هذا التكليف لم يعد مسودة إدارية.")

    entries = list(assignment.entries.select_related("target_node"))
    if not entries:
        raise ValidationError("لا يمكن إصدار تكليف بدون بنود.")

    visit_node_ids = set(visit.nodes.values_list("id", flat=True))
    for entry in entries:
        node = entry.target_node
        if node.id not in visit_node_ids:
            raise ValidationError(
                f"العنصر {node.code} لم يعد جزءًا من الزيارة؛ لا يمكن الإصدار."
            )
        if node.visit_id != visit.id:
            raise ValidationError(f"العنصر {node.code} لا ينتمي إلى هذه الزيارة.")
        if not (entry.scope_locked or entry.completion_required):
            raise ValidationError(f"البند {node.code} لا يفرض أي التزام.")

    for entry in entries:
        _materialise_entry(visit, entry, actor)

    assignment.status = AssignmentStatus.ISSUED
    assignment.issued_by = actor
    assignment.issued_at = timezone.now()
    assignment.save(update_fields=["status", "issued_by", "issued_at", "updated_at"])
    return assignment


def _materialise_entry(visit: Visit, entry: AssignmentEntry, actor) -> None:
    """Bring the targeted element into the scope without overwriting its origin."""
    node = entry.target_node
    fields: dict = {"is_selected": True, "status": NodeStatus.ACTIVE,
                    "excluded_at": None, "excluded_via": None}
    if node.origin is None:
        fields["origin"] = Origin.ASSIGNMENT
        node.origin = Origin.ASSIGNMENT
    VisitNode.objects.filter(pk=node.pk).update(**fields)
    node.is_selected = True
    node.status = NodeStatus.ACTIVE

    if node.is_branch:
        for child in _branch_descendants(visit, node):
            child_fields: dict = {
                "is_selected": True,
                "status": NodeStatus.ACTIVE,
                "excluded_at": None,
                "excluded_via": None,
            }
            if child.origin is None:
                child_fields["origin"] = Origin.ASSIGNMENT
                child.origin = Origin.ASSIGNMENT
            VisitNode.objects.filter(pk=child.pk).update(**child_fields)
            child.is_selected = True
            child.status = NodeStatus.ACTIVE


@transaction.atomic
def revoke_assignment(*, assignment: Assignment, actor, reason: str) -> Assignment:
    """Revoke an issued assignment while the visit is still a draft.

    The audit fields are recorded and nothing is deleted. Obligations that this
    assignment contributed disappear from the *effective* constraint set, but any
    constraint that came from the baseline or from another issued assignment
    remains in force.
    """
    if not reason.strip():
        raise ValidationError("سبب الإلغاء مطلوب.")
    visit = assignment.visit
    if not visit.is_draft:
        raise ValidationError(
            "لا يمكن إلغاء تكليف على زيارة مكتملة: لا نعيد تفسير الماضي."
        )
    if assignment.status != AssignmentStatus.ISSUED:
        raise ValidationError("يمكن إلغاء التكليفات الصادرة فقط.")

    assignment.status = AssignmentStatus.REVOKED
    assignment.revoked_by = actor
    assignment.revoked_at = timezone.now()
    assignment.revoke_reason = reason
    assignment.save(
        update_fields=[
            "status",
            "revoked_by",
            "revoked_at",
            "revoke_reason",
            "updated_at",
        ]
    )
    return assignment