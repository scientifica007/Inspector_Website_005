"""Deterministic, versioned JSON export of a visit.

Design rules that make the output comparable across runs:

* no wall-clock timestamps are emitted except those that are *historical facts*
  (creation, completion, issue and revocation times), and every one of them is a
  stored value rather than "now";
* ordering is explicit everywhere, so two exports of an unchanged visit are
  byte-identical after JSON serialisation;
* the export reads only from the visit's own frozen snapshot plus the audit rows
  that belong to it, never from the live reference, guide or institution.
"""

from __future__ import annotations

from typing import Any

from governance.constraints import effective_constraints
from governance.models import (
    Assignment,
    AssignmentStatus,
    GuideApplication,
)

from .models import NodeStatus, Visit, VisitNode
from .services import compute_progress

EXPORT_SCHEMA = "inspector.visit.export"
EXPORT_VERSION = 2


def _dt(value) -> str | None:
    return value.isoformat() if value else None


def _node_payload(node: VisitNode, depth_by_id: dict[int, int]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": node.code,
        "node_type": node.node_type,
        "title": node.title,
        "body": node.body,
        "parent_code": node.parent.code if node.parent_id else None,
        "depth": depth_by_id.get(node.id, 0),
        "position": node.position,
        "role": "SELECTED" if node.is_selected else "CONTEXT",
        "status": node.status,
        "origin": node.origin,
        "value_type": node.value_type,
        "is_completable": node.is_completable,
    }
    if node.is_description and node.value_type != "NONE":
        payload["value"] = _value_payload(node)
    if node.is_checklist_item:
        payload["result"] = node.result or None
    if node.observation:
        payload["observation"] = node.observation
    if node.is_excluded:
        payload["excluded_at"] = _dt(node.excluded_at)
        payload["excluded_via"] = (
            node.excluded_via.code if node.excluded_via_id else None
        )
    return payload


def _value_payload(node: VisitNode) -> dict[str, Any]:
    if node.value_type == "NUMBER":
        # Normalised so ``12`` and ``12.000`` serialise identically.
        value = (
            format(node.value_number.normalize(), "f")
            if node.value_number is not None
            else None
        )
        return {"type": "NUMBER", "value": value}
    if node.value_type == "DATE":
        return {"type": "DATE", "value": _dt(node.value_date)}
    if node.value_type == "BOOLEAN":
        return {"type": "BOOLEAN", "value": node.value_boolean}
    return {"type": "TEXT", "value": node.value_text or None}


def _depth_map(nodes: list[VisitNode]) -> dict[int, int]:
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

    for node in nodes:
        depth_of(node)
    return depths


def _ordered_nodes(visit: Visit) -> list[VisitNode]:
    """Depth-first, (position, id) within each level: same order the UI shows."""
    nodes = list(
        visit.nodes.select_related("parent").order_by("position", "id")
    )
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


def _assignment_payload(assignment: Assignment) -> dict[str, Any]:
    entries = sorted(
        assignment.entries.select_related("target_node"),
        key=lambda entry: (entry.target_node.position, entry.target_node.id),
    )
    return {
        "title": assignment.title,
        "instruction": assignment.instruction,
        "status": assignment.status,
        "created_at": _dt(assignment.created_at),
        "issued_by": assignment.issued_by.username if assignment.issued_by_id else None,
        "issued_at": _dt(assignment.issued_at),
        "revoked_by": assignment.revoked_by.username if assignment.revoked_by_id else None,
        "revoked_at": _dt(assignment.revoked_at),
        "revoke_reason": assignment.revoke_reason or None,
        "entries": [
            {
                "target_code": entry.target_node.code,
                "target_title": entry.target_node.title,
                "scope_locked": entry.scope_locked,
                "completion_required": entry.completion_required,
                "note": entry.note or None,
            }
            for entry in entries
        ],
    }


def _guide_application_payload(application: GuideApplication) -> dict[str, Any]:
    return {
        "guide_title": application.guide_title_snapshot,
        "guide_version": application.guide_version_snapshot,
        "guide_still_exists": application.guide_id is not None,
        "applied_by": application.applied_by.username if application.applied_by_id else None,
        "applied_at": _dt(application.created_at),
        "added_count": application.added_count,
        "skipped_codes": sorted(application.skipped_codes or []),
    }


def build_visit_export(visit: Visit) -> dict[str, Any]:
    """Build the full export dictionary for a visit."""
    nodes = _ordered_nodes(visit)
    depth_by_id = _depth_map(nodes)
    constraints = effective_constraints(visit)
    progress = compute_progress(visit)

    node_payloads = [_node_payload(node, depth_by_id) for node in nodes]

    baseline_scope_locked = sorted(
        node.code for node in nodes if node.baseline_scope_locked
    )
    baseline_completion_required = sorted(
        node.code for node in nodes if node.baseline_completion_required
    )
    effective_scope_locked = sorted(
        node.code for node in nodes if constraints.is_scope_locked(node)
    )
    effective_completion_required = sorted(
        node.code for node in nodes if constraints.is_completion_required(node)
    )

    assignments = sorted(
        visit.assignments.all(),
        key=lambda a: (a.created_at, a.id),
    )

    return {
        "schema": EXPORT_SCHEMA,
        "version": EXPORT_VERSION,
        "visit": {
            "title": visit.title,
            "status": visit.status,
            "visit_date": _dt(visit.visit_date),
            "created_at": _dt(visit.created_at),
            "completed_at": _dt(visit.completed_at),
            "completed_by": visit.completed_by.username if visit.completed_by_id else None,
            "notes": visit.notes or None,
            "inspector": visit.inspector.username,
            "institution": {
                "name_snapshot": visit.institution_name_snapshot,
                "current_name": visit.institution.name,
                "kind": visit.institution.kind,
            },
            "source_reference": {
                "title_snapshot": visit.source_reference_title or None,
                "visibility_snapshot": visit.source_reference_visibility or None,
                "revision_snapshot": visit.source_reference_revision,
                "still_exists": visit.source_reference_id is not None,
                "current_revision": (
                    visit.source_reference.revision
                    if visit.source_reference_id
                    else None
                ),
            },
        },
        "scope": {
            "selected_count": sum(1 for n in node_payloads if n["role"] == "SELECTED"),
            "context_count": sum(1 for n in node_payloads if n["role"] == "CONTEXT"),
            "active_count": sum(1 for n in node_payloads if n["status"] == NodeStatus.ACTIVE),
            "excluded_count": sum(1 for n in node_payloads if n["status"] == NodeStatus.EXCLUDED),
            "nodes": node_payloads,
        },
        "progress": {
            "done": progress.done,
            "total": progress.total,
            "percent": progress.percent,
        },
        "constraints": {
            "baseline": {
                "scope_locked": baseline_scope_locked,
                "completion_required": baseline_completion_required,
            },
            "effective": {
                "scope_locked": effective_scope_locked,
                "completion_required": effective_completion_required,
            },
        },
        "guide_applications": [
            _guide_application_payload(application)
            for application in sorted(
                visit.guide_applications.select_related("guide", "applied_by"),
                key=lambda a: (a.created_at, a.id),
            )
        ],
        "assignments": [
            _assignment_payload(assignment) for assignment in assignments
        ],
        "assignment_audit": {
            "issued_count": sum(
                1 for a in assignments if a.status != AssignmentStatus.DRAFT
            ),
            "revoked_count": sum(
                1 for a in assignments if a.status == AssignmentStatus.REVOKED
            ),
        },
    }