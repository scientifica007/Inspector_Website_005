"""Constraint resolution: baseline + effective.

Two layers are kept apart on purpose:

* **Baseline** constraints are intrinsic to the frozen snapshot. They come from
  the reference itself (a node marked mandatory or scope-locked) and from the
  visit's own local authoring choices. They exist independently of any
  assignment.
* **Effective** constraints are what actually applies right now: the baseline
  plus every obligation contributed by *currently issued* assignments.

Revoking an assignment therefore removes only what that assignment added. A
constraint that was present before the assignment survives its revocation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from governance.models import Assignment, AssignmentEntry, AssignmentStatus
from visits.models import NodeStatus, Visit, VisitNode


def _descendant_ids(visit: Visit) -> dict[int, list[int]]:
    """Map each node id to the ids of all its descendants."""
    nodes = list(visit.nodes.only("id", "parent_id"))
    children: dict[int | None, list[int]] = {}
    for node in nodes:
        children.setdefault(node.parent_id, []).append(node.id)

    result: dict[int, list[int]] = {}

    def collect(node_id: int) -> list[int]:
        if node_id in result:
            return result[node_id]
        acc: list[int] = []
        for child_id in children.get(node_id, []):
            acc.append(child_id)
            acc.extend(collect(child_id))
        result[node_id] = acc
        return acc

    for node in nodes:
        collect(node.id)
    return result


@dataclass
class EffectiveConstraints:
    """Resolved constraint sets for one visit."""

    visit: Visit
    #: node id -> True when scope-locked (baseline or issued assignment).
    scope_locked_ids: set[int] = field(default_factory=set)
    #: node id -> True when completion is required (baseline or assignment).
    completion_required_ids: set[int] = field(default_factory=set)
    #: Contribution of every *issued* entry, kept for revocation math.
    issued_entry_targets: dict[int, set[int]] = field(default_factory=dict)

    def is_scope_locked(self, node: VisitNode) -> bool:
        return node.id in self.scope_locked_ids

    def is_completion_required(self, node: VisitNode) -> bool:
        return node.id in self.completion_required_ids

    def baseline_scope_locked(self, node: VisitNode) -> bool:
        return node.baseline_scope_locked

    def baseline_completion_required(self, node: VisitNode) -> bool:
        return node.baseline_completion_required


def effective_constraints(visit: Visit) -> EffectiveConstraints:
    """Resolve baseline and assignment constraints into one view."""
    descendants = _descendant_ids(visit)
    nodes = {node.id: node for node in visit.nodes.all()}

    result = EffectiveConstraints(visit=visit)

    # --- Baseline ----------------------------------------------------------
    for node in nodes.values():
        covered = [node.id] + descendants.get(node.id, [])
        if node.baseline_scope_locked:
            result.scope_locked_ids.update(covered)
        if node.baseline_completion_required:
            # A branch is structural and cannot itself be fulfilled, so a
            # mandatory branch means: every completable descendant is required.
            result.completion_required_ids.update(covered)

    # --- Issued assignment obligations -------------------------------------
    entries = AssignmentEntry.objects.filter(
        assignment__visit=visit,
        assignment__status=AssignmentStatus.ISSUED,
    ).values_list("id", "target_node_id", "scope_locked", "completion_required")

    for entry_id, target_id, scope_locked, completion_required in entries:
        covered = [target_id] + descendants.get(target_id, [])
        if covered:
            result.issued_entry_targets.setdefault(entry_id, set()).update(covered)
        if scope_locked:
            result.scope_locked_ids.update(covered)
        if completion_required:
            result.completion_required_ids.update(covered)

    # Completion requirements only ever apply to elements that can actually be
    # fulfilled; a purely structural node never blocks completion.
    result.completion_required_ids = {
        node_id
        for node_id in result.completion_required_ids
        if node_id in nodes and nodes[node_id].is_completable
    }
    return result


def assignment_targets_in_subtree(visit: Visit, node: VisitNode) -> list[int]:
    descendants = _descendant_ids(visit)
    return [node.id] + descendants.get(node.id, [])