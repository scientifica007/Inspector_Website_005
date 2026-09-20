"""Visit lifecycle services.

Division of responsibility:

* snapping a live reference into an immutable visit tree,
* building and mutating the selective scope (add / soft-exclude / restore),
* recording execution data,
* guarding completion.

Everything here assumes the caller already passed permission checks; it does not
re-check who the actor is beyond ownership rules that are part of the domain
(such as "a visit belongs to one inspector").
"""

from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date

from catalog.models import NodeType, Reference, ReferenceNode, ValueType
from catalog.services import generate_local_code, ordered_tree

from .models import (
    ItemResult,
    NodeStatus,
    Origin,
    Visit,
    VisitNode,
    VisitStatus,
)


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------
def _coerce_date(value):
    """Accept a date or an ISO string so callers cannot store a bare string.

    A DateField assigned a ``str`` saves fine in SQLite but comes back as a
    string, which breaks serialisation later. Normalising here keeps the
    boundary honest for service, seed and test callers alike.
    """
    if isinstance(value, str):
        parsed = parse_date(value)
        if parsed is None:
            raise ValidationError("تاريخ الزيارة غير صالح.")
        return parsed
    return value


def build_frozen_nodes(reference: Reference) -> list[dict]:
    """Flatten a reference into plain dicts ready to become visit nodes."""
    tree = ordered_tree(reference)
    return tree


@transaction.atomic
def create_visit(
    *,
    actor,
    institution,
    visit_date,
    title: str = "",
    reference: Reference | None = None,
    notes: str = "",
) -> Visit:
    """Create a draft visit, optionally freezing a reference snapshot.

    The new visit starts with an **empty scope**: freezing copies the structure
    for reference, but no node is ``is_selected`` yet. Only roots are selected
    by default when the inspector explicitly seeds them later.
    """
    if reference is not None and not (
        reference.is_shared or reference.owner_id == actor.id
    ):
        raise ValidationError("لا يمكنك إنشاء زيارة من مرجع لا تملكه.")

    visit_date = _coerce_date(visit_date)

    visit = Visit.objects.create(
        title=title or f"زيارة {institution.name}",
        institution=institution,
        inspector=actor,
        visit_date=visit_date,
        notes=notes,
        source_reference=reference,
        source_reference_title=reference.title if reference else "",
        source_reference_visibility=reference.visibility if reference else "",
        source_reference_revision=reference.revision if reference else None,
        institution_name_snapshot=institution.name,
    )

    if reference is not None:
        _snapshot_reference_into(visit, reference)
    return visit


def _snapshot_reference_into(visit: Visit, reference: Reference) -> None:
    tree = build_frozen_nodes(reference)
    id_map: dict[int, VisitNode] = {}
    for raw in tree:
        parent = id_map.get(raw["parent_id"])
        node = VisitNode.objects.create(
            visit=visit,
            parent=parent,
            node_type=raw["node_type"],
            code=raw["code"],
            source_node_id=raw["id"],
            title=raw["title"],
            body=raw["body"],
            value_type=raw["value_type"],
            position=raw["position"],
            # Origin is written when an element enters the *scope*, so a freshly
            # snapshotted node has none yet: it exists as available structure.
            origin=None,
            is_selected=False,
            status=NodeStatus.ACTIVE,
            baseline_scope_locked=raw.get("is_scope_locked", False),
            baseline_completion_required=raw.get("is_mandatory", False),
        )
        id_map[raw["id"]] = node


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------
def nodes_by_code(visit: Visit) -> dict[str, VisitNode]:
    return {node.code: node for node in visit.nodes.all()}


def _descendants(node: VisitNode) -> list[VisitNode]:
    """All descendants of a node, breadth-first, deterministic."""
    result: list[VisitNode] = []
    frontier = [node]
    while frontier:
        children = list(
            VisitNode.objects.filter(parent__in=frontier).order_by("position", "id")
        )
        result.extend(children)
        frontier = children
    return result


def context_ancestors(node: VisitNode) -> list[VisitNode]:
    """Ancestors needed to give a deeply selected node its context.

    These stay ``is_selected=False``: showing them is not the same as inspecting
    them, so they never count towards progress.
    """
    ancestors: list[VisitNode] = []
    current = node.parent
    while current is not None:
        ancestors.append(current)
        current = current.parent
    return list(reversed(ancestors))


@transaction.atomic
def select_node(
    *, visit: Visit, node: VisitNode, origin: str = Origin.MANUAL
) -> VisitNode:
    """Bring a node (and, for branches, its subtree) into the scope.

    ``origin`` is recorded only for elements that were not already in the scope.
    If an element already entered through MANUAL, applying a Guide or an
    Assignment later must not erase that original reason.
    """
    _assert_draft(visit)
    if node.visit_id != visit.id:
        raise ValidationError("العنصر لا ينتمي إلى هذه الزيارة.")

    # Guard against a stale instance: callers frequently hold the object across
    # several service calls (especially from views).
    node.refresh_from_db(fields=["is_selected", "origin", "status"])

    # Ancestors of a deep node are rendered as context automatically: they stay
    # is_selected=False, so they never count towards progress merely for showing.
    _mark_selected(visit, node, origin)

    if node.is_branch:
        for child in _descendants(node):
            _mark_selected(visit, child, origin)

    _revive_if_excluded(visit, node, include_descendants=node.is_branch)
    return node


def _mark_selected(visit: Visit, node: VisitNode, origin: str) -> None:
    fields: dict = {}
    if not node.is_selected:
        fields["is_selected"] = True
        node.is_selected = True
    # Origin is written once; a later Guide/Assignment never overwrites it.
    if node.origin is None:
        fields["origin"] = origin
        node.origin = origin
    if fields:
        VisitNode.objects.filter(pk=node.pk).update(**fields)


@transaction.atomic
def add_local_node(
    *,
    visit: Visit,
    node_type: str,
    title: str,
    body: str = "",
    parent: VisitNode | None = None,
    value_type: str = ValueType.NONE,
) -> VisitNode:
    """Author content that lives only inside this visit."""
    _assert_draft(visit)
    if parent is not None and parent.visit_id != visit.id:
        raise ValidationError("العقدة الأم تنتمي لزيارة أخرى.")
    position = (
        VisitNode.objects.filter(visit=visit, parent=parent)
        .order_by("-position")
        .values_list("position", flat=True)
        .first()
        or 0
    )
    node = VisitNode.objects.create(
        visit=visit,
        parent=parent,
        node_type=node_type,
        code=generate_local_code(prefix="LOCAL"),
        title=title,
        body=body,
        value_type=value_type,
        position=position + 10,
        origin=Origin.LOCAL,
        is_selected=True,
        status=NodeStatus.ACTIVE,
    )
    if parent is not None:
        for ancestor in context_ancestors(node):
            VisitNode.objects.filter(pk=ancestor.pk).update(is_selected=True)
    return node


@transaction.atomic
def exclude_node(*, visit: Visit, node: VisitNode) -> None:
    """Soft exclusion: keep all recorded data, only take it out of the scope."""
    _assert_draft(visit)
    if node.visit_id != visit.id:
        raise ValidationError("العنصر لا ينتمي إلى هذه الزيارة.")

    now = timezone.now()
    VisitNode.objects.filter(pk=node.pk).update(
        status=NodeStatus.EXCLUDED,
        is_selected=False,
        excluded_at=now,
        excluded_via=None,
    )
    for child in _descendants(node):
        # A descendant excluded explicitly earlier keeps its own exclusion: its
        # ``excluded_via`` stays None so restoring the ancestor does not silently
        # revive it. Only currently-active descendants are grouped under ``node``.
        VisitNode.objects.filter(pk=child.pk, status=NodeStatus.ACTIVE).update(
            status=NodeStatus.EXCLUDED,
            is_selected=False,
            excluded_at=now,
            excluded_via=node,
        )


@transaction.atomic
def restore_node(*, visit: Visit, node: VisitNode) -> None:
    """Undo an exclusion without touching results or observations."""
    _assert_draft(visit)
    if node.visit_id != visit.id:
        raise ValidationError("العنصر لا ينتمي إلى هذه الزيارة.")
    # Re-read: callers may hold a stale instance after a previous service call.
    node.refresh_from_db(fields=["status"])
    if not node.is_excluded:
        return

    VisitNode.objects.filter(pk=node.pk).update(
        status=NodeStatus.ACTIVE,
        is_selected=True,
        excluded_at=None,
        excluded_via=None,
    )
    # Only descendants excluded *through this node* come back; ones excluded by
    # an explicit earlier action stay excluded.
    for child in VisitNode.objects.filter(excluded_via=node):
        VisitNode.objects.filter(pk=child.pk).update(
            status=NodeStatus.ACTIVE,
            is_selected=True,
            excluded_at=None,
            excluded_via=None,
        )


@transaction.atomic
def deselect_node(*, visit: Visit, node: VisitNode) -> None:
    """Remove an element from the scope without marking it excluded."""
    _assert_draft(visit)
    _assert_not_scope_locked(visit, node)
    if node.is_branch:
        for child in _descendants(node):
            VisitNode.objects.filter(pk=child.pk).update(is_selected=False)
    VisitNode.objects.filter(pk=node.pk).update(is_selected=False)


def _assert_not_scope_locked(visit: Visit, node: VisitNode) -> None:
    from governance.constraints import effective_constraints

    constraints = effective_constraints(visit)
    if constraints.is_scope_locked(node):
        raise ValidationError(
            "لا يمكن إخراج هذا العنصر من النطاق: يوجد قيد نطاق سارٍ عليه."
        )


def _revive_if_excluded(visit: Visit, node: VisitNode, include_descendants: bool) -> None:
    if node.is_excluded:
        VisitNode.objects.filter(pk=node.pk).update(
            status=NodeStatus.ACTIVE, excluded_at=None, excluded_via=None
        )
    if include_descendants:
        VisitNode.objects.filter(visit=visit, excluded_via=node).update(
            status=NodeStatus.ACTIVE, excluded_at=None, excluded_via=None
        )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def _assert_draft(visit: Visit) -> None:
    if not visit.is_draft:
        raise ValidationError("لا يمكن تعديل زيارة مكتملة. الحقيقة التاريخية محمية.")


@transaction.atomic
def record_result(
    *, visit: Visit, node: VisitNode, result: str, observation: str | None = None
) -> VisitNode:
    _assert_draft(visit)
    if node.visit_id != visit.id:
        raise ValidationError("العنصر لا ينتمي إلى هذه الزيارة.")
    if not node.is_checklist_item:
        raise ValidationError("النتيجة تُسجَّل لبنود التفتيش فقط.")
    if result and result not in ItemResult.values:
        raise ValidationError("نتيجة غير معروفة.")
    updates = {"result": result}
    node.result = result
    if observation is not None:
        updates["observation"] = observation
        node.observation = observation
    VisitNode.objects.filter(pk=node.pk).update(**updates)
    return node


@transaction.atomic
def record_value(
    *,
    visit: Visit,
    node: VisitNode,
    value_text: str | None = None,
    value_number=None,
    value_date=None,
    value_boolean=None,
    observation: str | None = None,
) -> VisitNode:
    _assert_draft(visit)
    if node.visit_id != visit.id:
        raise ValidationError("العنصر لا ينتمي إلى هذه الزيارة.")
    if not node.carries_value:
        raise ValidationError("هذا العنصر لا يحمل قيمة.")
    updates = {
        "value_text": value_text if value_text is not None else node.value_text,
        "value_number": value_number if value_number is not None else node.value_number,
        "value_date": value_date if value_date is not None else node.value_date,
        "value_boolean": value_boolean if value_boolean is not None else node.value_boolean,
    }
    if observation is not None:
        updates["observation"] = observation
    VisitNode.objects.filter(pk=node.pk).update(**updates)
    for key, value in updates.items():
        setattr(node, key, value)
    return node


# ---------------------------------------------------------------------------
# Progress and completion
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Progress:
    done: int
    total: int

    @property
    def percent(self) -> int:
        if self.total == 0:
            return 100
        return int(round(self.done * 100 / self.total))


def compute_progress(visit: Visit) -> Progress:
    counted = [
        node
        for node in visit.nodes.filter(status=NodeStatus.ACTIVE, is_selected=True)
        if node.is_completable
    ]
    done = sum(1 for node in counted if node.is_fulfilled)
    return Progress(done=done, total=len(counted))


def outstanding_requirements(visit: Visit) -> list[VisitNode]:
    """Active, selected elements that are still expected to carry evidence."""
    from governance.constraints import effective_constraints

    constraints = effective_constraints(visit)
    missing: list[VisitNode] = []
    for node in visit.nodes.filter(status=NodeStatus.ACTIVE):
        if not constraints.is_completion_required(node):
            continue
        if not node.is_completable:
            continue
        if node.is_fulfilled:
            continue
        missing.append(node)
    return missing


@transaction.atomic
def complete_visit(*, visit: Visit, actor) -> Visit:
    _assert_draft(visit)
    outstanding = outstanding_requirements(visit)
    if outstanding:
        codes = "، ".join(node.code for node in outstanding[:10])
        raise ValidationError(
            "لا يمكن إكمال الزيارة: توجد متطلبات إتمام غير منجزة (" + codes + ")."
        )
    visit.status = VisitStatus.COMPLETED
    visit.completed_at = timezone.now()
    visit.completed_by = actor
    visit.save(update_fields=["status", "completed_at", "completed_by", "updated_at"])
    return visit


@transaction.atomic
def delete_draft(*, visit: Visit, actor) -> None:
    """Drafts are deletable by their owner only.

    Decision: an administrator cannot delete another inspector's draft. The
    draft carries the inspector's in-progress professional work, and ownership
    is the whole point of the draft/completed distinction. Administrators can
    see drafts they were assigned to, but removal stays with the author.
    """
    if visit.inspector_id != actor.id:
        raise ValidationError("لا يمكنك حذف مسودة لا تملكها.")
    if not visit.is_draft:
        raise ValidationError("لا يمكن حذف زيارة مكتملة.")
    from governance.models import Assignment, AssignmentStatus

    if Assignment.objects.filter(
        visit=visit, status=AssignmentStatus.ISSUED
    ).exists():
        raise ValidationError(
            "لا يمكن حذف هذه المسودة: يوجد تكليف رسمي صادر مرتبط بها."
        )
    visit.delete()