"""Guide and Assignment tests.

The two concepts are deliberately tested side by side, because the most
important property is the difference between them: a guide suggests, an
assignment orders.
"""

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.testing import (
    add_branch,
    add_item,
    make_admin,
    make_assignment,
    make_guide,
    make_inspector,
    make_reference_tree,
    make_shared_reference,
    make_visit,
)
from governance.constraints import effective_constraints
from governance.models import (
    Assignment,
    AssignmentEntry,
    AssignmentStatus,
    Guide,
    GuideApplication,
    GuideStatus,
)
from governance.services import (
    add_assignment_entry,
    add_guide_target,
    apply_guide,
    create_assignment,
    create_guide,
    issue_assignment,
    revoke_assignment,
)
from visits.models import ItemResult, Origin
from visits.services import (
    compute_progress,
    exclude_node,
    outstanding_requirements,
    record_result,
    select_node,
)


class GuideApplicationTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.inspector = make_inspector()
        self.reference = make_reference_tree(self.admin)
        self.visit = make_visit(self.inspector, self.reference)
        self.guide = make_guide(
            self.admin, self.reference, title="دليل", codes=["B1-D-1", "B1-D-2"]
        )

    def node(self, code):
        return self.visit.nodes.get(code=code)

    def test_apply_adds_the_suggested_items(self):
        outcome = apply_guide(visit=self.visit, guide=self.guide, actor=self.inspector)
        self.assertEqual(sorted(outcome.added), ["B1-D-1", "B1-D-2"])
        self.assertTrue(self.node("B1-D-1").is_selected)
        self.assertTrue(self.node("B1-D-2").is_selected)

    def test_applied_items_get_the_guide_origin(self):
        apply_guide(visit=self.visit, guide=self.guide, actor=self.inspector)
        self.assertEqual(self.node("B1-D-1").origin, Origin.GUIDE)

    def test_a_guide_does_not_lock_elements(self):
        apply_guide(visit=self.visit, guide=self.guide, actor=self.inspector)
        constraints = effective_constraints(self.visit)
        self.assertFalse(constraints.is_scope_locked(self.node("B1-D-1")))

    def test_a_guide_does_not_make_items_completion_required(self):
        apply_guide(visit=self.visit, guide=self.guide, actor=self.inspector)
        constraints = effective_constraints(self.visit)
        self.assertFalse(constraints.is_completion_required(self.node("B1-D-1")))

    def test_guide_can_be_deselected_freely(self):
        from visits.services import deselect_node

        apply_guide(visit=self.visit, guide=self.guide, actor=self.inspector)
        deselect_node(visit=self.visit, node=self.node("B1-D-1"))
        self.assertFalse(self.node("B1-D-1").is_selected)

    def test_application_is_idempotent(self):
        first = apply_guide(visit=self.visit, guide=self.guide, actor=self.inspector)
        self.assertEqual(len(first.added), 2)
        second = apply_guide(visit=self.visit, guide=self.guide, actor=self.inspector)
        self.assertEqual(second.added, [])
        self.assertEqual(GuideApplication.objects.filter(visit=self.visit).count(), 1)

    def test_reapplying_after_manual_deselection_adds_only_missing(self):
        from visits.services import deselect_node

        apply_guide(visit=self.visit, guide=self.guide, actor=self.inspector)
        deselect_node(visit=self.visit, node=self.node("B1-D-1"))
        outcome = apply_guide(visit=self.visit, guide=self.guide, actor=self.inspector)
        self.assertEqual(outcome.added, ["B1-D-1"])
        self.assertTrue(self.node("B1-D-2").is_selected)

    def test_manual_origin_is_preserved_when_a_guide_touches_the_same_item(self):
        select_node(visit=self.visit, node=self.node("B1-D-1"))
        apply_guide(visit=self.visit, guide=self.guide, actor=self.inspector)
        self.assertEqual(self.node("B1-D-1").origin, Origin.MANUAL)
        self.assertEqual(self.node("B1-D-2").origin, Origin.GUIDE)

    def test_guide_cannot_be_applied_to_a_visit_from_another_reference(self):
        other_reference = make_reference_tree(self.admin)
        visit = make_visit(self.inspector, other_reference)
        with self.assertRaises(ValidationError):
            apply_guide(visit=visit, guide=self.guide, actor=self.inspector)

    def test_unpublished_guide_cannot_be_applied(self):
        draft = make_guide(
            self.admin, self.reference, title="مسودة", codes=["B1-D-1"],
            status=GuideStatus.DRAFT,
        )
        with self.assertRaises(ValidationError):
            apply_guide(visit=self.visit, guide=draft, actor=self.inspector)

    def test_guide_only_accepts_codes_present_in_its_reference(self):
        with self.assertRaises(ValidationError):
            add_guide_target(guide=self.guide, code="GHOST")

    def test_guide_target_duplicate_is_refused(self):
        with self.assertRaises(ValidationError):
            add_guide_target(guide=self.guide, code="B1-D-1")

    def test_changing_the_guide_after_application_does_not_rewrite_the_visit(self):
        apply_guide(visit=self.visit, guide=self.guide, actor=self.inspector)
        self.guide.title = "دليل مُغيَّر"
        self.guide.save()

        # Remove the other target from the guide; the visit keeps what it added.
        self.guide.targets.filter(code="B1-D-2").delete()
        self.assertTrue(self.node("B1-D-2").is_selected)

    def test_deleting_the_guide_after_application_keeps_the_visit_and_the_audit(self):
        apply_guide(visit=self.visit, guide=self.guide, actor=self.inspector)
        guide_pk = self.guide.pk
        self.guide.delete()

        self.assertTrue(self.node("B1-D-1").is_selected)
        application = GuideApplication.objects.get(visit=self.visit)
        self.assertIsNone(application.guide_id)
        self.assertEqual(application.guide_title_snapshot, "دليل")
        self.assertFalse(Guide.objects.filter(pk=guide_pk).exists())

    def test_reference_change_bumps_revision_without_touching_applied_visit(self):
        apply_guide(visit=self.visit, guide=self.guide, actor=self.inspector)
        add_item(self.reference, "LATER", "بند لاحق",
                 parent=self.reference.nodes.get(code="B1"))
        self.assertFalse(self.visit.nodes.filter(code="LATER").exists())


class GuideTemporalMismatchTests(TestCase):
    """A guide whose targets left the frozen snapshot must degrade gracefully."""

    def setUp(self):
        self.admin = make_admin()
        self.inspector = make_inspector()
        self.reference = make_reference_tree(self.admin)
        self.visit = make_visit(self.inspector, self.reference)

    def test_target_missing_from_the_snapshot_is_skipped_not_pulled_from_live(self):
        # The guide knows about an item the snapshot does not contain: this models
        # "guide edited after the visit snapshot was taken".
        guide = make_guide(self.admin, self.reference, title="دليل", codes=["B1-D-1"])
        add_item(self.reference, "BRAND-NEW", "بند جديد",
                 parent=self.reference.nodes.get(code="B1"))
        add_guide_target(guide=guide, code="BRAND-NEW")

        outcome = apply_guide(visit=self.visit, guide=guide, actor=self.inspector)
        self.assertEqual(outcome.added, ["B1-D-1"])
        self.assertEqual(outcome.skipped, ["BRAND-NEW"])
        self.assertFalse(self.visit.nodes.filter(code="BRAND-NEW").exists())

    def test_skipped_codes_are_recorded_on_the_application(self):
        guide = make_guide(self.admin, self.reference, title="دليل", codes=["B1-D-1"])
        add_item(self.reference, "BRAND-NEW", "بند جديد",
                 parent=self.reference.nodes.get(code="B1"))
        add_guide_target(guide=guide, code="BRAND-NEW")
        apply_guide(visit=self.visit, guide=guide, actor=self.inspector)
        application = GuideApplication.objects.get(visit=self.visit)
        self.assertEqual(application.skipped_codes, ["BRAND-NEW"])
        self.assertEqual(application.skipped_count, 1)


class GuidePolicyTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.inspector = make_inspector()
        self.reference = make_reference_tree(self.admin)

    def test_a_guide_must_belong_to_a_shared_reference(self):
        private = make_shared_reference(self.admin, "خاصة")
        private.visibility = "PRIVATE"
        private.owner = self.admin
        private.save()
        with self.assertRaises(ValidationError):
            create_guide(actor=self.admin, reference=private, title="دليل")

    def test_inspector_cannot_create_or_apply_guides_for_others(self):
        visit = make_visit(self.inspector, self.reference)
        guide = make_guide(self.admin, self.reference, title="دليل", codes=["B1-D-1"])
        other = make_inspector("other")
        self.client.force_login(other)
        response = self.client.post(
            reverse("governance:guide_apply", args=[visit.pk, guide.pk])
        )
        self.assertEqual(response.status_code, 403)

    def test_inspector_cannot_open_the_guide_admin(self):
        self.client.force_login(self.inspector)
        response = self.client.get(reverse("governance:guide_list"))
        self.assertEqual(response.status_code, 403)


class AssignmentWorkflowTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.inspector = make_inspector()
        self.reference = make_reference_tree(self.admin)
        self.visit = make_visit(self.inspector, self.reference)

    def node(self, code):
        return self.visit.nodes.get(code=code)

    def test_draft_assignment_is_invisible_to_the_inspector(self):
        assignment = create_assignment(
            visit=self.visit, actor=self.admin, title="ZZ-ADMIN-ONLY-TITLE"
        )
        add_assignment_entry(
            assignment=assignment, target_node=self.node("B1-D-1"),
            scope_locked=True,
        )
        self.client.force_login(self.inspector)
        response = self.client.get(reverse("governance:my_assignments"))
        self.assertEqual(list(response.context["assignments"]), [])

        response = self.client.get(reverse("visits:detail", args=[self.visit.pk]))
        self.assertNotContains(response, "ZZ-ADMIN-ONLY-TITLE")

    def test_draft_assignment_constraints_are_not_effective(self):
        assignment = create_assignment(
            visit=self.visit, actor=self.admin, title="تكليف مسودة"
        )
        add_assignment_entry(
            assignment=assignment, target_node=self.node("B1-D-1"),
            scope_locked=True,
        )
        constraints = effective_constraints(self.visit)
        self.assertFalse(constraints.is_scope_locked(self.node("B1-D-1")))

    def test_issued_assignment_is_visible_to_the_inspector(self):
        assignment = make_assignment(
            self.admin, self.visit, title="تكليف رسمي",
            entries=[("B1-D-1", True, False)],
        )
        issue_assignment(assignment=assignment, actor=self.admin)
        self.client.force_login(self.inspector)
        response = self.client.get(reverse("governance:my_assignments"))
        self.assertEqual(len(list(response.context["assignments"])), 1)

    def test_issue_brings_the_target_into_scope_with_assignment_origin(self):
        assignment = make_assignment(
            self.admin, self.visit, entries=[("B1-D-1", False, True)]
        )
        issue_assignment(assignment=assignment, actor=self.admin)
        node = self.node("B1-D-1")
        self.assertTrue(node.is_selected)
        self.assertEqual(node.origin, Origin.ASSIGNMENT)

    def test_issue_does_not_overwrite_an_existing_origin(self):
        select_node(visit=self.visit, node=self.node("B1-D-1"))
        assignment = make_assignment(
            self.admin, self.visit, entries=[("B1-D-1", True, True)]
        )
        issue_assignment(assignment=assignment, actor=self.admin)
        self.assertEqual(self.node("B1-D-1").origin, Origin.MANUAL)

    def test_issue_is_atomic_when_any_entry_is_invalid(self):
        assignment = make_assignment(
            self.admin, self.visit, title="تكليف",
            entries=[("B1-D-1", True, False)],
        )
        # Corrupt one entry so that validation must fail. Using update() bypasses
        # the model layer, which is exactly what a stale/dirty row looks like.
        bad = self.node("B1-D-1")
        entry = assignment.entries.first()
        entry.target_node = bad
        entry.save()
        AssignmentEntry.objects.filter(pk=entry.pk).update(target_node=bad)

        # Make the second entry point at a node from another visit.
        other_visit = make_visit(self.inspector, self.reference)
        foreign_node = other_visit.nodes.get(code="B1-D-2")
        AssignmentEntry.objects.filter(pk=entry.pk).update(target_node=foreign_node)

        with self.assertRaises(ValidationError):
            issue_assignment(assignment=assignment, actor=self.admin)

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, AssignmentStatus.DRAFT)
        self.assertIsNone(assignment.issued_at)
        # No partial application happened.
        self.assertFalse(self.node("B1-D-1").is_selected)

    def test_issue_without_entries_is_refused(self):
        assignment = create_assignment(
            visit=self.visit, actor=self.admin, title="تكليف"
        )
        with self.assertRaises(ValidationError):
            issue_assignment(assignment=assignment, actor=self.admin)

    def test_entries_cannot_be_modified_after_issue(self):
        assignment = make_assignment(
            self.admin, self.visit, entries=[("B1-D-1", True, False)]
        )
        issue_assignment(assignment=assignment, actor=self.admin)
        with self.assertRaises(ValidationError):
            add_assignment_entry(
                assignment=assignment, target_node=self.node("B1-D-2"),
                completion_required=True,
            )

    def test_assignment_cannot_be_created_on_a_completed_visit(self):
        node = self.node("B1-D-2")
        select_node(visit=self.visit, node=node)
        record_result(visit=self.visit, node=node, result=ItemResult.MATCH)
        from visits.services import complete_visit

        complete_visit(visit=self.visit, actor=self.inspector)
        self.visit.refresh_from_db()
        with self.assertRaises(ValidationError):
            create_assignment(visit=self.visit, actor=self.admin, title="متأخر")

    def test_completion_required_blocks_the_visit(self):
        assignment = make_assignment(
            self.admin, self.visit, entries=[("B1-D-1", False, True)]
        )
        issue_assignment(assignment=assignment, actor=self.admin)
        from visits.services import complete_visit

        with self.assertRaises(ValidationError):
            complete_visit(visit=self.visit, actor=self.inspector)

    def test_completion_required_is_satisfied_by_recording_a_result(self):
        assignment = make_assignment(
            self.admin, self.visit, entries=[("B1-D-1", False, True)]
        )
        issue_assignment(assignment=assignment, actor=self.admin)
        record_result(visit=self.visit, node=self.node("B1-D-1"), result=ItemResult.MATCH)
        # The reference-mandatory item B1-D-2 must also be satisfied.
        mandatory = self.node("B1-D-2")
        select_node(visit=self.visit, node=mandatory)
        record_result(visit=self.visit, node=mandatory, result=ItemResult.MATCH)
        self.assertEqual(outstanding_requirements(self.visit), [])
        from visits.services import complete_visit

        complete_visit(visit=self.visit, actor=self.inspector)
        self.visit.refresh_from_db()
        self.assertTrue(self.visit.is_completed)

    def test_completion_required_on_a_branch_applies_to_completable_descendants(self):
        assignment = make_assignment(
            self.admin, self.visit, entries=[("B1", False, True)]
        )
        issue_assignment(assignment=assignment, actor=self.admin)
        constraints = effective_constraints(self.visit)
        # The branch itself is structural: it must not be required.
        self.assertFalse(constraints.is_completion_required(self.node("B1")))
        # Its completable descendants are.
        self.assertTrue(constraints.is_completion_required(self.node("B1-D-1")))
        self.assertTrue(constraints.is_completion_required(self.node("B1-1")))

    def test_scope_locked_branch_covers_its_subtree(self):
        assignment = make_assignment(
            self.admin, self.visit, entries=[("B1", True, False)]
        )
        issue_assignment(assignment=assignment, actor=self.admin)
        constraints = effective_constraints(self.visit)
        for code in ["B1", "B1-D", "B1-D-1", "B1-1"]:
            self.assertTrue(constraints.is_scope_locked(self.node(code)))

    def test_scope_locked_branch_cannot_be_deselected(self):
        from visits.services import deselect_node

        assignment = make_assignment(
            self.admin, self.visit, entries=[("B1", True, False)]
        )
        issue_assignment(assignment=assignment, actor=self.admin)
        with self.assertRaises(ValidationError):
            deselect_node(visit=self.visit, node=self.node("B1"))
        with self.assertRaises(ValidationError):
            deselect_node(visit=self.visit, node=self.node("B1-D-1"))

    def test_scope_locked_branch_cannot_be_excluded_via_http(self):
        assignment = make_assignment(
            self.admin, self.visit, entries=[("B1", True, False)]
        )
        issue_assignment(assignment=assignment, actor=self.admin)
        self.client.force_login(self.inspector)
        response = self.client.post(
            reverse("visits:scope_exclude", args=[self.visit.pk, self.node("B1-D-1").pk])
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.node("B1-D-1").is_excluded)


class AssignmentOverlapTests(TestCase):
    """Two assignments can constrain the same element independently."""

    def setUp(self):
        self.admin = make_admin()
        self.inspector = make_inspector()
        self.reference = make_reference_tree(self.admin)
        self.visit = make_visit(self.inspector, self.reference)

    def node(self, code):
        return self.visit.nodes.get(code=code)

    def test_second_assignment_does_not_duplicate_the_target(self):
        first = make_assignment(self.admin, self.visit, title="أ",
                                entries=[("B1-D-1", True, False)])
        issue_assignment(assignment=first, actor=self.admin)
        second = make_assignment(self.admin, self.visit, title="ب",
                                 entries=[("B1-D-1", False, True)])
        issue_assignment(assignment=second, actor=self.admin)

        constraints = effective_constraints(self.visit)
        self.assertTrue(constraints.is_scope_locked(self.node("B1-D-1")))
        self.assertTrue(constraints.is_completion_required(self.node("B1-D-1")))

    def test_revoking_one_keeps_the_other_obligation(self):
        first = make_assignment(self.admin, self.visit, title="أ",
                                entries=[("B1-D-1", True, False)])
        issue_assignment(assignment=first, actor=self.admin)
        second = make_assignment(self.admin, self.visit, title="ب",
                                 entries=[("B1-D-1", False, True)])
        issue_assignment(assignment=second, actor=self.admin)

        revoke_assignment(assignment=first, actor=self.admin, reason="لم يعد لازمًا")

        constraints = effective_constraints(self.visit)
        self.assertFalse(constraints.is_scope_locked(self.node("B1-D-1")))
        self.assertTrue(constraints.is_completion_required(self.node("B1-D-1")))

    def test_revocation_does_not_erase_a_baseline_constraint(self):
        # B1-D-2 is mandatory in the reference, so the visit has a baseline
        # completion requirement on it before any assignment exists.
        assignment = make_assignment(self.admin, self.visit, title="تكليف",
                                     entries=[("B1-D-2", False, True)])
        issue_assignment(assignment=assignment, actor=self.admin)
        revoke_assignment(assignment=assignment, actor=self.admin, reason="إلغاء")

        constraints = effective_constraints(self.visit)
        self.assertTrue(constraints.is_completion_required(self.node("B1-D-2")))
        self.assertTrue(self.node("B1-D-2").baseline_completion_required)

    def test_revocation_removes_only_that_assignment_obligation(self):
        assignment = make_assignment(self.admin, self.visit, title="تكليف",
                                     entries=[("B1-D-1", False, True)])
        issue_assignment(assignment=assignment, actor=self.admin)
        revoke_assignment(assignment=assignment, actor=self.admin, reason="سبب")

        constraints = effective_constraints(self.visit)
        self.assertFalse(constraints.is_completion_required(self.node("B1-D-1")))

    def test_revocation_does_not_remove_the_element_from_the_scope(self):
        assignment = make_assignment(self.admin, self.visit, title="تكليف",
                                     entries=[("B1-D-1", True, True)])
        issue_assignment(assignment=assignment, actor=self.admin)
        revoke_assignment(assignment=assignment, actor=self.admin, reason="سبب")
        node = self.node("B1-D-1")
        self.assertTrue(node.is_selected)
        self.assertEqual(node.origin, Origin.ASSIGNMENT)

    def test_revocation_records_the_audit_trail(self):
        assignment = make_assignment(self.admin, self.visit, title="تكليف",
                                     entries=[("B1-D-1", True, False)])
        issue_assignment(assignment=assignment, actor=self.admin)
        revoke_assignment(assignment=assignment, actor=self.admin, reason="سبب واضح")
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, AssignmentStatus.REVOKED)
        self.assertEqual(assignment.revoked_by, self.admin)
        self.assertIsNotNone(assignment.revoked_at)
        self.assertEqual(assignment.revoke_reason, "سبب واضح")

    def test_revocation_requires_a_reason(self):
        assignment = make_assignment(self.admin, self.visit, title="تكليف",
                                     entries=[("B1-D-1", True, False)])
        issue_assignment(assignment=assignment, actor=self.admin)
        with self.assertRaises(ValidationError):
            revoke_assignment(assignment=assignment, actor=self.admin, reason="   ")

    def test_revocation_is_refused_on_a_completed_visit(self):
        node = self.node("B1-D-2")
        select_node(visit=self.visit, node=node)
        record_result(visit=self.visit, node=node, result=ItemResult.MATCH)

        assignment = make_assignment(self.admin, self.visit, title="تكليف",
                                     entries=[("B1-D-1", True, False)])
        issue_assignment(assignment=assignment, actor=self.admin)

        record_result(visit=self.visit, node=self.node("B1-D-1"), result=ItemResult.MATCH)
        from visits.services import complete_visit

        complete_visit(visit=self.visit, actor=self.inspector)
        self.visit.refresh_from_db()

        with self.assertRaises(ValidationError):
            revoke_assignment(assignment=assignment, actor=self.admin, reason="محاولة")
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, AssignmentStatus.ISSUED)

    def test_revoked_assignment_cannot_be_reissued(self):
        assignment = make_assignment(self.admin, self.visit, title="تكليف",
                                     entries=[("B1-D-1", True, False)])
        issue_assignment(assignment=assignment, actor=self.admin)
        revoke_assignment(assignment=assignment, actor=self.admin, reason="سبب")
        with self.assertRaises(ValidationError):
            issue_assignment(assignment=assignment, actor=self.admin)


class AssignmentPermissionTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.inspector = make_inspector()
        self.reference = make_reference_tree(self.admin)
        self.visit = make_visit(self.inspector, self.reference)

    def test_inspector_cannot_create_an_assignment(self):
        self.client.force_login(self.inspector)
        response = self.client.get(
            reverse("governance:assignment_create", args=[self.visit.pk])
        )
        self.assertEqual(response.status_code, 403)

    def test_inspector_cannot_revoke_an_assignment(self):
        assignment = make_assignment(self.admin, self.visit, title="تكليف",
                                     entries=[("B1-D-1", True, False)])
        issue_assignment(assignment=assignment, actor=self.admin)
        self.client.force_login(self.inspector)
        response = self.client.post(
            reverse("governance:assignment_revoke", args=[assignment.pk]),
            {"reason": "لا"},
        )
        self.assertEqual(response.status_code, 403)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, AssignmentStatus.ISSUED)

    def test_admin_cannot_create_an_assignment_on_a_completed_visit(self):
        node = self.visit.nodes.get(code="B1-D-2")
        select_node(visit=self.visit, node=node)
        record_result(visit=self.visit, node=node, result=ItemResult.MATCH)
        from visits.services import complete_visit

        complete_visit(visit=self.visit, actor=self.inspector)
        self.client.force_login(self.admin)
        response = self.client.get(
            reverse("governance:assignment_create", args=[self.visit.pk])
        )
        self.assertEqual(response.status_code, 302)

    def test_admin_issuing_via_http_records_the_actor(self):
        assignment = make_assignment(self.admin, self.visit, title="تكليف",
                                     entries=[("B1-D-1", True, False)])
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("governance:assignment_issue", args=[assignment.pk])
        )
        self.assertEqual(response.status_code, 302)
        assignment.refresh_from_db()
        self.assertEqual(assignment.issued_by, self.admin)
        self.assertEqual(assignment.status, AssignmentStatus.ISSUED)

    def test_atomic_issue_failure_leaves_the_assignment_as_draft_via_http(self):
        assignment = create_assignment(
            visit=self.visit, actor=self.admin, title="تكليف"
        )
        other_visit = make_visit(self.inspector, self.reference)
        foreign = other_visit.nodes.get(code="B1-D-1")
        AssignmentEntry.objects.create(
            assignment=assignment, target_node=foreign, scope_locked=True
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("governance:assignment_issue", args=[assignment.pk])
        )
        self.assertEqual(response.status_code, 302)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, AssignmentStatus.DRAFT)