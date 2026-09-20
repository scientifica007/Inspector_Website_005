"""Shared test factories.

Real model and service calls only; nothing here mocks the code under test.
"""

from __future__ import annotations

from accounts.models import Role, User
from catalog.models import NodeType, Reference, ReferenceVisibility, ValueType
from catalog.services import add_node, create_reference
from governance.models import Assignment, AssignmentStatus, Guide, GuideStatus, GuideTarget
from institutions.models import Institution, InstitutionKind
from visits.models import Visit
from visits.services import create_visit


def make_admin(username="admin", password="pass-12345") -> User:
    return User.objects.create_user(
        username=username, password=password, role=Role.ADMIN
    )


def make_inspector(username="inspector", password="pass-12345") -> User:
    return User.objects.create_user(
        username=username, password=password, role=Role.INSPECTOR
    )


def make_institution(name="معهد الاختبار", code="", **kwargs) -> Institution:
    kwargs.setdefault("kind", InstitutionKind.TRAINING_CENTER)
    if not code:
        code = f"INST-{Institution.objects.count() + 1}"
    return Institution.objects.create(name=name, code=code, **kwargs)


def make_shared_reference(actor, title="مرجع التفتيش البيداغوجي") -> Reference:
    return create_reference(
        actor=actor, title=title, visibility=ReferenceVisibility.SHARED
    )


def make_private_reference(actor, title="مرجع خاص") -> Reference:
    return create_reference(
        actor=actor,
        title=title,
        visibility=ReferenceVisibility.PRIVATE,
        owner=actor,
    )


def add_branch(reference, code, title="فرع", parent=None, **kwargs):
    return add_node(
        reference=reference,
        node_type=NodeType.BRANCH,
        code=code,
        title=title,
        parent=parent,
        **kwargs,
    )


def add_description(reference, code, title="وصف", parent=None,
                    value_type=ValueType.NONE, **kwargs):
    return add_node(
        reference=reference,
        node_type=NodeType.DESCRIPTION,
        code=code,
        title=title,
        parent=parent,
        value_type=value_type,
        **kwargs,
    )


def add_item(reference, code, title="بند", parent=None, **kwargs):
    return add_node(
        reference=reference,
        node_type=NodeType.CHECKLIST_ITEM,
        code=code,
        title=title,
        parent=parent,
        **kwargs,
    )


def make_reference_tree(actor):
    """A small but realistically shaped reference.

    root
      ├── B1 فرع أول
      │     ├── B1-D وصف
      │     │     ├── B1-D-1 بند
      │     │     └── B1-D-2 بند (mandatory in the reference)
      │     └── B1-1 بند
      └── B2 فرع ثان
            └── B2-1 بند
    """
    reference = make_shared_reference(actor)
    root = add_branch(reference, "ROOT", "الجذر")
    b1 = add_branch(reference, "B1", "فرع أول", parent=root)
    desc = add_description(reference, "B1-D", "وصف أول", parent=b1)
    add_item(reference, "B1-D-1", "بند أول", parent=desc)
    add_item(reference, "B1-D-2", "بند إلزامي", parent=desc, is_mandatory=True)
    add_item(reference, "B1-1", "بند عام", parent=b1)
    b2 = add_branch(reference, "B2", "فرع ثان", parent=root)
    add_item(reference, "B2-1", "بند ثانٍ", parent=b2)
    return reference


def make_visit(actor, reference=None, institution=None):
    institution = institution or make_institution()
    return create_visit(
        actor=actor,
        institution=institution,
        visit_date="2026-01-15",
        reference=reference,
    )


def make_guide(actor, reference, title="دليل", codes=(), status=GuideStatus.PUBLISHED):
    guide = Guide.objects.create(
        title=title, reference=reference, created_by=actor, status=status
    )
    for index, code in enumerate(codes):
        node = reference.nodes.get(code=code)
        GuideTarget.objects.create(
            guide=guide,
            code=code,
            title_snapshot=node.title,
            node_type_snapshot=node.node_type,
            position=(index + 1) * 10,
        )
    return guide


def make_assignment(actor, visit, title="تكليف", entries=()):
    """Create a draft assignment with entries ``(code, scope_locked, completion_required)``."""
    assignment = Assignment.objects.create(
        visit=visit, title=title, created_by=actor, status=AssignmentStatus.DRAFT
    )
    by_code = {node.code: node for node in visit.nodes.all()}
    from governance.models import AssignmentEntry

    for code, scope_locked, completion_required in entries:
        AssignmentEntry.objects.create(
            assignment=assignment,
            target_node=by_code[code],
            scope_locked=scope_locked,
            completion_required=completion_required,
        )
    return assignment


__all__ = [
    "make_admin",
    "make_inspector",
    "make_institution",
    "make_shared_reference",
    "make_private_reference",
    "add_branch",
    "add_description",
    "add_item",
    "make_reference_tree",
    "make_visit",
    "make_guide",
    "make_assignment",
    "Visit",
]