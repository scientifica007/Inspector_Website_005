"""Cross-app workflow test through the real view layer.

These tests drive the HTTP endpoints an inspector and an administrator actually
use, rather than calling services directly. They are the regression net for the
whole product contract: reference → visit → scope → guide → assignment →
completion → export.
"""

import json

from django.test import TestCase
from django.urls import reverse

from catalog.models import ValueType
from core.testing import (
    add_branch,
    add_description,
    add_item,
    make_admin,
    make_inspector,
    make_institution,
    make_shared_reference,
)
from governance.constraints import effective_constraints
from governance.models import GuideApplication, GuideStatus, GuideTarget
from governance.services import apply_guide, create_guide, issue_assignment, revoke_assignment
from visits.models import ItemResult, NodeStatus, Origin
from visits.services import (
    add_local_node,
    compute_progress,
    create_visit,
    record_result,
    select_node,
)


class WorkflowTestMixin:
    """Builds one shared reference plus a hand-picked visit."""

    def build_reference(self):
        self.reference = make_shared_reference(self.admin, "مرجع سير العمل")
        root = add_branch(self.reference, "ROOT", "الجذر")
        self.b1 = add_branch(self.reference, "B1", "فرع أول", parent=root)
        self.desc = add_description(self.reference, "B1-D", "وصف", parent=self.b1)
        self.item = add_item(self.reference, "B1-D-1", "بند أول", parent=self.desc)
        self.mandatory = add_item(
            self.reference, "B1-D-2", "بند إلزامي", parent=self.desc, is_mandatory=True
        )
        add_item(self.reference, "B1-1", "بند عام", parent=self.b1)

    def build_visit(self):
        self.visit = create_visit(
            actor=self.inspector,
            institution=self.institution,
            visit_date="2026-05-20",
            title="زيارة سير العمل",
            reference=self.reference,
        )

    def setUp(self):
        self.admin = make_admin()
        self.inspector = make_inspector()
        self.institution = make_institution("معهد سير العمل", code="WF")
        self.build_reference()
        self.build_visit()
        self.client.force_login(self.inspector)


class VisitCreationWorkflowTests(WorkflowTestMixin, TestCase):
    def test_inspector_creates_a_visit_through_the_form(self):
        response = self.client.post(
            reverse("visits:create"),
            {
                "title": "زيارة جديدة",
                "institution": self.institution.pk,
                "visit_date": "20/05/2026",
                "reference": self.reference.pk,
                "notes": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        created = self.inspector.visits.get(title="زيارة جديدة")
        # The date must be stored as a real date, not a string.
        self.assertEqual(created.visit_date.isoformat(), "2026-05-20")
        self.assertEqual(created.source_reference_label, "مرجع سير العمل")

    def test_new_visit_snapshot_copies_the_whole_reference(self):
        created = create_visit(
            actor=self.inspector,
            institution=self.institution,
            visit_date="2026-05-20",
            reference=self.reference,
        )
        codes = set(created.nodes.values_list("code", flat=True))
        self.assertEqual(codes, {"ROOT", "B1", "B1-D", "B1-D-1", "B1-D-2", "B1-1"})

    def test_new_visit_scope_is_empty_but_the_tree_is_visible(self):
        codes = set(self.visit.nodes.values_list("code", flat=True))
        self.assertEqual(len(codes), 6)
        self.assertFalse(self.visit.nodes.filter(is_selected=True).exists())

    def test_mandatory_reference_item_becomes_a_baseline_requirement(self):
        by_code = {n.code: n for n in self.visit.nodes.all()}
        self.assertTrue(by_code["B1-D-2"].baseline_completion_required)
        self.assertFalse(by_code["B1-D-1"].baseline_completion_required)

    def test_visit_without_a_reference_is_allowed(self):
        visit = create_visit(
            actor=self.inspector,
            institution=self.institution,
            visit_date="2026-05-20",
        )
        self.assertIsNone(visit.source_reference_id)
        self.assertEqual(visit.nodes.count(), 0)


class ScopeWorkflowTests(WorkflowTestMixin, TestCase):
    def test_select_endpoint_adds_a_deep_item_and_its_context(self):
        node = self.visit.nodes.get(code="B1-D-1")
        response = self.client.post(
            reverse("visits:scope_select", args=[self.visit.pk, node.pk])
        )
        self.assertEqual(response.status_code, 302)
        self.visit.refresh_from_db()
        node.refresh_from_db()
        self.assertTrue(node.is_selected)
        self.assertEqual(node.origin, Origin.MANUAL)
        # Ancestors are visible as context but are not selected.
        for code in ("ROOT", "B1", "B1-D"):
            ancestor = self.visit.nodes.get(code=code)
            self.assertTrue(ancestor.is_context_only)
            self.assertFalse(ancestor.is_selected)

    def test_progress_counts_only_selected_completable_items(self):
        select_node(visit=self.visit, node=self.visit.nodes.get(code="B1-D-1"))
        select_node(visit=self.visit, node=self.visit.nodes.get(code="B1-D-2"))
        select_node(visit=self.visit, node=self.visit.nodes.get(code="B1-D"))
        progress = compute_progress(self.visit)
        # B1-D is a description: not completable, so it is excluded.
        self.assertEqual(progress.total, 2)

    def test_exclude_and_restore_keep_the_recorded_result(self):
        node = self.visit.nodes.get(code="B1-D-1")
        select_node(visit=self.visit, node=node)
        record_result(
            visit=self.visit, node=node, result=ItemResult.NOT_MATCH,
            observation="ملاحظة مهمة",
        )
        self.client.post(reverse("visits:scope_exclude", args=[self.visit.pk, node.pk]))
        node.refresh_from_db()
        self.assertEqual(node.status, NodeStatus.EXCLUDED)
        self.assertEqual(node.result, ItemResult.NOT_MATCH)
        self.assertEqual(node.observation, "ملاحظة مهمة")
        self.assertEqual(compute_progress(self.visit).total, 0)

        self.client.post(reverse("visits:scope_restore", args=[self.visit.pk, node.pk]))
        node.refresh_from_db()
        self.assertEqual(node.status, NodeStatus.ACTIVE)
        self.assertEqual(node.result, ItemResult.NOT_MATCH)
        self.assertEqual(node.observation, "ملاحظة مهمة")

    def test_local_node_endpoint_creates_visit_scoped_content(self):
        # A checklist item must hang off a branch or description, so author the
        # container first, then the item beneath it.
        self.client.post(
            reverse("visits:local_node_create", args=[self.visit.pk]),
            {
                "node_type": "BRANCH",
                "title": "فرع محلي",
                "body": "",
                "value_type": ValueType.NONE,
            },
        )
        branch = self.visit.nodes.get(title="فرع محلي")
        self.assertEqual(branch.origin, Origin.LOCAL)
        self.assertTrue(branch.is_selected)

        response = self.client.post(
            reverse("visits:local_node_create", args=[self.visit.pk]),
            {
                "node_type": "CHECKLIST_ITEM",
                "title": "بند محلي",
                "body": "تفصيل محلي",
                "parent": branch.pk,
                "value_type": ValueType.NONE,
            },
        )
        self.assertEqual(response.status_code, 302)
        local = self.visit.nodes.get(title="بند محلي")
        self.assertEqual(local.origin, Origin.LOCAL)
        self.assertTrue(local.is_selected)
        self.assertEqual(local.parent_id, branch.pk)
        self.assertTrue(local.code.startswith("LOCAL-"))

    def test_orphan_checklist_item_is_rejected(self):
        before = self.visit.nodes.count()
        self.client.post(
            reverse("visits:local_node_create", args=[self.visit.pk]),
            {"node_type": "CHECKLIST_ITEM", "title": "بلا سياق", "body": "",
             "value_type": ValueType.NONE},
        )
        self.assertEqual(self.visit.nodes.count(), before)


class GuideWorkflowTests(WorkflowTestMixin, TestCase):
    def make_published_guide(self, codes):
        guide = create_guide(
            actor=self.admin,
            reference=self.reference,
            title="دليل السير",
            description="",
        )
        for index, code in enumerate(codes):
            node = self.reference.nodes.get(code=code)
            GuideTarget.objects.create(
                guide=guide,
                code=code,
                title_snapshot=node.title,
                node_type_snapshot=node.node_type,
                position=(index + 1) * 10,
            )
        guide.status = GuideStatus.PUBLISHED
        guide.save(update_fields=["status"])
        return guide

    def test_applying_a_guide_adds_missing_targets_with_origin_guide(self):
        guide = self.make_published_guide(["B1-D-1", "B1-1"])
        outcome = apply_guide(visit=self.visit, guide=guide,
                                       actor=self.inspector)
        self.assertCountEqual(outcome.added, ["B1-D-1", "B1-1"])
        node = self.visit.nodes.get(code="B1-D-1")
        self.assertEqual(node.origin, Origin.GUIDE)
        self.assertTrue(node.is_selected)

    def test_guide_application_is_idempotent_and_preserves_manual_origin(self):
        node = self.visit.nodes.get(code="B1-D-1")
        select_node(visit=self.visit, node=node)
        guide = self.make_published_guide(["B1-D-1", "B1-1"])

        first = apply_guide(visit=self.visit, guide=guide,
                                     actor=self.inspector)
        self.assertCountEqual(first.added, ["B1-1"])
        self.assertCountEqual(first.skipped, ["B1-D-1"])
        node.refresh_from_db()
        self.assertEqual(node.origin, Origin.MANUAL)

        second = apply_guide(visit=self.visit, guide=guide,
                                      actor=self.inspector)
        self.assertEqual(second.added, [])
        self.assertTrue(second.already_applied)
        self.assertEqual(
            GuideApplication.objects.filter(visit=self.visit, guide=guide).count(), 1
        )

    def test_guide_never_locks_scope_or_requires_completion(self):
        guide = self.make_published_guide(["B1-D-1"])
        apply_guide(visit=self.visit, guide=guide, actor=self.inspector)
        constraints = effective_constraints(self.visit)
        node = self.visit.nodes.get(code="B1-D-1")
        self.assertFalse(constraints.is_scope_locked(node))
        self.assertFalse(constraints.is_completion_required(node))

    def test_guide_target_missing_from_snapshot_is_skipped(self):
        # The live reference gained an element after the visit was frozen.
        add_item(self.reference, "B1-NEW", "بند جديد", parent=self.b1)
        guide = self.make_published_guide(["B1-NEW", "B1-D-1"])
        outcome = apply_guide(visit=self.visit, guide=guide,
                                       actor=self.inspector)
        self.assertEqual(outcome.skipped, ["B1-NEW"])
        self.assertEqual(outcome.added, ["B1-D-1"])
        self.assertFalse(self.visit.nodes.filter(code="B1-NEW").exists())

    def test_guide_cannot_be_applied_to_a_foreign_reference_visit(self):
        from django.core.exceptions import ValidationError

        other = make_shared_reference(self.admin, "مرجع آخر")
        guide = create_guide(
            actor=self.admin, reference=other, title="دليل آخر", description=""
        )
        guide.status = GuideStatus.PUBLISHED
        guide.save(update_fields=["status"])
        with self.assertRaises(ValidationError):
            apply_guide(visit=self.visit, guide=guide,
                                 actor=self.inspector)


class AssignmentWorkflowTests(WorkflowTestMixin, TestCase):
    def test_issued_assignment_applies_lock_and_requirement_without_origin_change(self):
        node = self.visit.nodes.get(code="B1-1")
        select_node(visit=self.visit, node=node)
        assignment = self._assignment_for("B1-1", scope_locked=True,
                                          completion_required=True)
        issue_assignment(assignment=assignment, actor=self.admin)
        node.refresh_from_db()
        self.assertEqual(node.origin, Origin.MANUAL)
        constraints = effective_constraints(self.visit)
        self.assertTrue(constraints.is_scope_locked(node))
        self.assertTrue(constraints.is_completion_required(node))

    def test_scope_locked_item_cannot_be_excluded_but_can_be_deselected(self):
        node = self.visit.nodes.get(code="B1-1")
        assignment = self._assignment_for("B1-1", scope_locked=True,
                                          completion_required=False)
        issue_assignment(assignment=assignment, actor=self.admin)
        self.client.post(reverse("visits:scope_exclude", args=[self.visit.pk, node.pk]))
        node.refresh_from_db()
        self.assertEqual(node.status, NodeStatus.ACTIVE)
        self.assertTrue(node.is_selected)

    def test_completion_is_blocked_by_an_unmet_assignment_requirement(self):
        assignment = self._assignment_for("B1-D-2", scope_locked=True,
                                          completion_required=True)
        issue_assignment(assignment=assignment, actor=self.admin)
        response = self.client.post(
            reverse("visits:complete", args=[self.visit.pk]), follow=True
        )
        self.visit.refresh_from_db()
        self.assertTrue(self.visit.is_draft)

        node = self.visit.nodes.get(code="B1-D-2")
        record_result(visit=self.visit, node=node, result=ItemResult.MATCH)
        self.client.post(reverse("visits:complete", args=[self.visit.pk]))
        self.visit.refresh_from_db()
        self.assertTrue(self.visit.is_completed)

    def test_revoking_an_assignment_keeps_the_baseline_requirement(self):
        assignment = self._assignment_for("B1-D-2", scope_locked=True,
                                          completion_required=True)
        issue_assignment(assignment=assignment, actor=self.admin)
        revoke_assignment(assignment=assignment, actor=self.admin, reason="خطأ إداري")
        constraints = effective_constraints(self.visit)
        node = self.visit.nodes.get(code="B1-D-2")
        # The reference itself demands this item: the baseline survives.
        self.assertTrue(constraints.is_completion_required(node))
        # The scope lock came only from the revoked assignment.
        self.assertFalse(constraints.is_scope_locked(node))

    def test_overlapping_assignments_survive_independent_revocation(self):
        node = self.visit.nodes.get(code="B1-1")
        lock = self._assignment_for("B1-1", scope_locked=True,
                                    completion_required=False, title="قيد")
        require = self._assignment_for("B1-1", scope_locked=False,
                                       completion_required=True, title="متطلب")
        issue_assignment(assignment=lock, actor=self.admin)
        issue_assignment(assignment=require, actor=self.admin)
        revoke_assignment(assignment=lock, actor=self.admin, reason="انتهى")

        constraints = effective_constraints(self.visit)
        self.assertFalse(constraints.is_scope_locked(node))
        self.assertTrue(constraints.is_completion_required(node))

    def _assignment_for(self, code, *, scope_locked, completion_required,
                        title="تكليف"):
        from governance.models import Assignment, AssignmentEntry, AssignmentStatus

        assignment = Assignment.objects.create(
            visit=self.visit, title=title, created_by=self.admin,
            status=AssignmentStatus.DRAFT,
        )
        AssignmentEntry.objects.create(
            assignment=assignment,
            target_node=self.visit.nodes.get(code=code),
            scope_locked=scope_locked,
            completion_required=completion_required,
        )
        return assignment


class CompletionWorkflowTests(WorkflowTestMixin, TestCase):
    def complete(self):
        node = self.visit.nodes.get(code="B1-D-2")
        select_node(visit=self.visit, node=node)
        record_result(visit=self.visit, node=node, result=ItemResult.MATCH)
        self.client.post(reverse("visits:complete", args=[self.visit.pk]))
        self.visit.refresh_from_db()

    def test_completed_visit_blocks_scope_endpoints(self):
        self.complete()
        node = self.visit.nodes.get(code="B1-1")
        response = self.client.post(
            reverse("visits:scope_select", args=[self.visit.pk, node.pk])
        )
        self.assertEqual(response.status_code, 403)
        response = self.client.get(reverse("visits:scope", args=[self.visit.pk]))
        self.assertEqual(response.status_code, 403)

    def test_completed_visit_blocks_execution_endpoints(self):
        self.complete()
        node = self.visit.nodes.get(code="B1-D-2")
        response = self.client.post(
            reverse("visits:record_item", args=[self.visit.pk, node.pk]),
            {"result": "NOT_MATCH", "observation": ""},
        )
        self.assertEqual(response.status_code, 403)
        node.refresh_from_db()
        self.assertEqual(node.result, ItemResult.MATCH)

    def test_completed_visit_cannot_be_deleted(self):
        self.complete()
        response = self.client.post(
            reverse("visits:delete", args=[self.visit.pk]), follow=True
        )
        # Deleting a completed visit is refused as a business rule: the request
        # is understood, the record is simply no longer deletable.
        self.assertTrue(type(self.visit).objects.filter(pk=self.visit.pk).exists())
        self.assertContains(response, "لا يمكن حذف زيارة مكتملة")

    def test_draft_visit_can_be_deleted_by_its_owner(self):
        response = self.client.post(reverse("visits:delete", args=[self.visit.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(type(self.visit).objects.filter(pk=self.visit.pk).exists())

    def test_another_inspector_cannot_delete_a_foreign_draft(self):
        other = make_inspector("other")
        self.client.force_login(other)
        response = self.client.post(reverse("visits:delete", args=[self.visit.pk]))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(type(self.visit).objects.filter(pk=self.visit.pk).exists())

    def test_admin_cannot_delete_an_inspector_draft(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("visits:delete", args=[self.visit.pk]))
        self.assertEqual(response.status_code, 403)

    def test_change_in_the_live_reference_does_not_alter_a_completed_visit(self):
        self.complete()
        node = self.visit.nodes.get(code="B1-D-2")
        original_title = node.title
        reference_node = self.reference.nodes.get(code="B1-D-2")
        reference_node.title = "عنوان متغير"
        reference_node.save()
        self.reference.nodes.get(code="B1-D-2").is_mandatory = False
        self.reference.nodes.get(code="B1-D-2").save()
        node.refresh_from_db()
        self.assertEqual(node.title, original_title)
        self.assertTrue(node.baseline_completion_required)

    def test_completed_visit_blocks_new_assignments(self):
        self.complete()
        from governance.models import Assignment, AssignmentEntry
        from governance.models import AssignmentStatus

        assignment = Assignment.objects.create(
            visit=self.visit, title="متأخر", created_by=self.admin,
            status=AssignmentStatus.DRAFT,
        )
        AssignmentEntry.objects.create(
            assignment=assignment,
            target_node=self.visit.nodes.get(code="B1-1"),
            scope_locked=True,
        )
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            issue_assignment(assignment=assignment, actor=self.admin)


class ExportWorkflowTests(WorkflowTestMixin, TestCase):
    def test_export_endpoint_returns_versioned_json(self):
        response = self.client.get(reverse("visits:export", args=[self.visit.pk]))
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertEqual(payload["schema"], "inspector.visit.export")
        self.assertIn("version", payload)
        self.assertEqual(payload["visit"]["status"], "DRAFT")

    def test_export_endpoint_uses_a_versioned_download_filename(self):
        response = self.client.get(
            reverse("visits:export", args=[self.visit.pk]), {"download": "1"}
        )
        self.assertEqual(
            response["Content-Disposition"],
            f'attachment; filename="visit-{self.visit.pk}-export-v2.json"',
        )

    def test_foreign_inspector_cannot_export(self):
        other = make_inspector("other")
        self.client.force_login(other)
        response = self.client.get(reverse("visits:export", args=[self.visit.pk]))
        self.assertEqual(response.status_code, 403)

    def test_admin_can_read_any_visit_export(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("visits:export", args=[self.visit.pk]))
        self.assertEqual(response.status_code, 200)