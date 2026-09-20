"""Reference library services.

Two concerns live here:

* building the canonical, deterministic tree/JSON representation of a reference
  (reused by the submission snapshot, the frozen visit snapshot and the export),
* the private-reference submission workflow, where approval creates a *new*
  shared reference and never mutates the private one.
"""

from __future__ import annotations

import uuid
from typing import Iterable

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import (
    NodeType,
    Reference,
    ReferenceNode,
    ReferenceSubmission,
    ReferenceVisibility,
    SubmissionStatus,
    ValueType,
)


def ordered_tree(reference: Reference) -> list[dict]:
    """Return the reference as a flat list ordered so parents precede children.

    Ordering is (position, id) within each level, depth-first. The output is
    fully deterministic which is what makes snapshot comparison meaningful.
    """
    nodes = list(
        reference.nodes.select_related("parent").order_by("position", "id")
    )
    by_parent: dict[int | None, list[ReferenceNode]] = {}
    for node in nodes:
        by_parent.setdefault(node.parent_id, []).append(node)

    result: list[dict] = []

    def walk(parent_id, depth: int) -> None:
        for node in by_parent.get(parent_id, []):
            result.append(_node_to_dict(node, depth))
            walk(node.id, depth + 1)

    walk(None, 0)
    return result


def _node_to_dict(node: ReferenceNode, depth: int) -> dict:
    return {
        "id": node.id,
        "parent_id": node.parent_id,
        "node_type": node.node_type,
        "code": node.code,
        "title": node.title,
        "body": node.body,
        "value_type": node.value_type,
        "is_mandatory": node.is_mandatory,
        "is_scope_locked": node.is_scope_locked,
        "position": node.position,
        "depth": depth,
    }


def reference_snapshot(reference: Reference) -> dict:
    """Deterministic JSON representation used for submissions and exports."""
    return {
        "reference_id": reference.id,
        "title": reference.title,
        "description": reference.description,
        "visibility": reference.visibility,
        "revision": reference.revision,
        "nodes": ordered_tree(reference),
    }


def node_codes(reference: Reference) -> set[str]:
    return set(reference.nodes.values_list("code", flat=True))


def generate_local_code(prefix: str = "LOCAL") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


def next_position(reference: Reference, parent_id: int | None) -> int:
    last = (
        reference.nodes.filter(parent_id=parent_id)
        .order_by("-position")
        .values_list("position", flat=True)
        .first()
    )
    return (last or 0) + 10


@transaction.atomic
def create_reference(
    *,
    actor,
    title: str,
    description: str = "",
    visibility: str = ReferenceVisibility.SHARED,
    owner=None,
) -> Reference:
    if visibility == ReferenceVisibility.PRIVATE and owner is None:
        owner = actor
    reference = Reference(
        title=title,
        description=description,
        visibility=visibility,
        owner=owner,
        created_by=actor,
    )
    reference.full_clean()
    reference.save()
    return reference


@transaction.atomic
def add_node(
    *,
    reference: Reference,
    node_type: str,
    title: str,
    body: str = "",
    code: str = "",
    parent: ReferenceNode | None = None,
    value_type: str = ValueType.NONE,
    is_mandatory: bool = False,
    is_scope_locked: bool = False,
    position: int | None = None,
) -> ReferenceNode:
    if not code:
        code = generate_local_code(prefix=node_type[:4])
    node = ReferenceNode(
        reference=reference,
        parent=parent,
        node_type=node_type,
        code=code,
        title=title,
        body=body,
        value_type=value_type,
        is_mandatory=is_mandatory,
        is_scope_locked=is_scope_locked,
        position=position if position is not None else next_position(reference, parent.id if parent else None),
    )
    node.full_clean()
    node.save()
    Reference.objects.filter(pk=reference.pk).update(revision=reference.revision + 1)
    reference.refresh_from_db(fields=["revision"])
    return node


@transaction.atomic
def touch_revision(reference: Reference, amount: int = 1) -> None:
    Reference.objects.filter(pk=reference.pk).update(
        revision=reference.revision + amount
    )
    reference.refresh_from_db(fields=["revision"])


@transaction.atomic
def soft_delete_reference(reference: Reference) -> None:
    """Hide a reference without destroying it.

    Visits already created keep their frozen snapshot, and guides keep a
    PROTECTed link, so a soft delete is the only safe global removal.
    """
    reference.is_active = False
    reference.deleted_at = timezone.now()
    reference.save(update_fields=["is_active", "deleted_at", "updated_at"])


@transaction.atomic
def restore_reference(reference: Reference) -> None:
    reference.is_active = True
    reference.deleted_at = None
    reference.save(update_fields=["is_active", "deleted_at", "updated_at"])


def can_delete_node(node: ReferenceNode, children: Iterable | None = None) -> bool:
    """Nodes may always be removed from the live reference.

    Historical visits hold frozen snapshots, so deleting a live node can never
    rewrite a past visit. The only cost is that guide targets referencing the
    deleted code become stale, which guide application reports and skips.
    """
    return True


@transaction.atomic
def create_submission(
    *, reference: Reference, actor, note: str = ""
) -> ReferenceSubmission:
    """Freeze the current private reference content for administrator review."""
    if not reference.is_private:
        raise ValidationError("يمكن إرسال المراجع الخاصة فقط للتعميم.")
    if reference.owner_id != actor.id:
        raise ValidationError("لا يمكن إرسال مرجع لا تملكه.")
    pending = reference.submissions.filter(status=SubmissionStatus.PENDING).exists()
    if pending:
        raise ValidationError("يوجد مقترح قيد المراجعة لهذا المرجع بالفعل.")
    submission = ReferenceSubmission.objects.create(
        reference=reference,
        submitted_by=actor,
        note=note,
        snapshot=reference_snapshot(reference),
    )
    return submission


@transaction.atomic
def approve_submission(*, submission: ReferenceSubmission, actor, note: str = "") -> Reference:
    """Create a brand new shared reference from the frozen submission content."""
    if not submission.is_pending:
        raise ValidationError("تمت مراجعة هذا المقترح مسبقًا.")
    snapshot = submission.snapshot
    shared = Reference.objects.create(
        title=snapshot.get("title", submission.reference.title),
        description=snapshot.get("description", ""),
        visibility=ReferenceVisibility.SHARED,
        owner=None,
        created_by=actor,
    )
    _materialise_nodes(shared, snapshot.get("nodes", []))

    submission.status = SubmissionStatus.APPROVED
    submission.reviewed_by = actor
    submission.reviewed_at = timezone.now()
    submission.review_note = note
    submission.approved_reference = shared
    submission.save(
        update_fields=[
            "status",
            "reviewed_by",
            "reviewed_at",
            "review_note",
            "approved_reference",
            "updated_at",
        ]
    )
    return shared


def _materialise_nodes(reference: Reference, nodes: list[dict]) -> None:
    """Recreate a snapshot tree, remapping parent ids without touching the source."""
    ordered = sorted(nodes, key=lambda n: (n.get("depth", 0), n.get("position", 0), n.get("id", 0)))
    id_map: dict[int, ReferenceNode] = {}
    for raw in ordered:
        parent = id_map.get(raw.get("parent_id"))
        node = ReferenceNode.objects.create(
            reference=reference,
            parent=parent,
            node_type=raw.get("node_type", NodeType.CHECKLIST_ITEM),
            code=raw.get("code") or generate_local_code(),
            title=raw.get("title", ""),
            body=raw.get("body", ""),
            value_type=raw.get("value_type", ValueType.NONE),
            is_mandatory=bool(raw.get("is_mandatory", False)),
            is_scope_locked=bool(raw.get("is_scope_locked", False)),
            position=int(raw.get("position", 0)),
        )
        id_map[raw["id"]] = node
    touch_revision(reference, len(ordered))


@transaction.atomic
def reject_submission(*, submission: ReferenceSubmission, actor, note: str = "") -> None:
    if not submission.is_pending:
        raise ValidationError("تمت مراجعة هذا المقترح مسبقًا.")
    submission.status = SubmissionStatus.REJECTED
    submission.reviewed_by = actor
    submission.reviewed_at = timezone.now()
    submission.review_note = note
    submission.save(
        update_fields=[
            "status",
            "reviewed_by",
            "reviewed_at",
            "review_note",
            "updated_at",
        ]
    )