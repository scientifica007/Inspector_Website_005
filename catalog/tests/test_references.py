"""Reference library tests: independence, node tree, deletion semantics."""

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from catalog.models import NodeType, Reference, ReferenceVisibility, ValueType
from catalog.services import (
    add_node,
    approve_submission,
    create_reference,
    create_submission,
    ordered_tree,
    reference_snapshot,
    reject_submission,
    restore_reference,
    soft_delete_reference,
    touch_revision,
)
from core.testing import (
    add_branch,
    add_description,
    add_item,
    make_admin,
    make_inspector,
    make_private_reference,
    make_reference_tree,
    make_shared_reference,
)


class ReferenceIndependenceTests(TestCase):
    """Several references coexist; none supersedes the others."""

    def setUp(self):
        self.admin = make_admin()

    def test_multiple_shared_references_are_valid_in_parallel(self):
        first = make_shared_reference(self.admin, "مرجع التفتيش البيداغوجي")
        second = make_shared_reference(self.admin, "مرجع متابعة التجهيزات")
        third = make_shared_reference(self.admin, "مرجع متابعة الدخول التكويني")

        self.assertEqual(Reference.objects.shared().count(), 3)
        # No "master" concept: all three stay active and equally selectable.
        self.assertTrue(all(r.is_active for r in [first, second, third]))
        self.assertEqual(len({r.pk for r in [first, second, third]}), 3)

    def test_same_node_code_allowed_in_different_references(self):
        first = make_shared_reference(self.admin, "مرجع أ")
        second = make_shared_reference(self.admin, "مرجع ب")
        add_branch(first, "NRM-01", "فرع")
        add_branch(second, "NRM-01", "فرع")
        self.assertEqual(first.nodes.count(), 1)
        self.assertEqual(second.nodes.count(), 1)

    def test_duplicate_node_code_rejected_within_one_reference(self):
        reference = make_shared_reference(self.admin)
        add_branch(reference, "DUP", "أول")
        with self.assertRaises(ValidationError):
            add_branch(reference, "DUP", "ثانٍ")


class ReferenceTreeTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.reference = make_reference_tree(self.admin)

    def test_hierarchy_shape(self):
        codes = [raw["code"] for raw in ordered_tree(self.reference)]
        self.assertEqual(
            codes, ["ROOT", "B1", "B1-D", "B1-D-1", "B1-D-2", "B1-1", "B2", "B2-1"]
        )

    def test_ordering_is_deterministic_across_calls(self):
        self.assertEqual(ordered_tree(self.reference), ordered_tree(self.reference))

    def test_depths_are_correct(self):
        depths = {raw["code"]: raw["depth"] for raw in ordered_tree(self.reference)}
        self.assertEqual(depths["ROOT"], 0)
        self.assertEqual(depths["B1"], 1)
        self.assertEqual(depths["B1-D"], 2)
        self.assertEqual(depths["B1-D-1"], 3)

    def test_checklist_item_cannot_be_root(self):
        with self.assertRaises(ValidationError):
            add_item(self.reference, "ORPHAN", "بند بلا أب")

    def test_branch_cannot_have_value_type(self):
        with self.assertRaises(ValidationError):
            add_node(
                reference=self.reference,
                node_type=NodeType.BRANCH,
                code="BAD-BRANCH",
                title="فرع بقيمة",
                value_type=ValueType.NUMBER,
            )

    def test_checklist_item_cannot_carry_value_type(self):
        parent = self.reference.nodes.get(code="B1")
        with self.assertRaises(ValidationError):
            add_node(
                reference=self.reference,
                node_type=NodeType.CHECKLIST_ITEM,
                code="BAD-ITEM",
                title="بند بقيمة",
                parent=parent,
                value_type=ValueType.TEXT,
            )

    def test_description_can_carry_a_value(self):
        parent = self.reference.nodes.get(code="B1")
        node = add_description(
            self.reference, "B1-V", "وصف بقيمة", parent=parent,
            value_type=ValueType.NUMBER,
        )
        self.assertTrue(node.carries_value)


class ReferenceRevisionTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.reference = make_shared_reference(self.admin)
        self.initial_revision = self.reference.revision

    def test_adding_a_node_bumps_the_revision(self):
        add_branch(self.reference, "N1", "فرع")
        self.reference.refresh_from_db()
        self.assertGreater(self.reference.revision, self.initial_revision)

    def test_touch_revision_is_explicit_and_idempotent_in_isolation(self):
        before = self.reference.revision
        touch_revision(self.reference)
        self.assertEqual(self.reference.revision, before + 1)


class ReferenceDeletionTests(TestCase):
    """Deleting from the live library never destroys history."""

    def setUp(self):
        self.admin = make_admin()
        self.reference = make_reference_tree(self.admin)

    def test_soft_delete_hides_but_keeps_the_row(self):
        pk = self.reference.pk
        soft_delete_reference(self.reference)
        self.reference.refresh_from_db()
        self.assertTrue(self.reference.is_deleted)
        self.assertFalse(self.reference.is_active)
        self.assertTrue(Reference.objects.filter(pk=pk).exists())
        self.assertEqual(self.reference.nodes.count(), 8)

    def test_soft_deleted_reference_is_excluded_by_live_but_kept_for_restore(self):
        soft_delete_reference(self.reference)
        # ``visible_to`` deliberately keeps soft-deleted rows so an administrator
        # can restore them; ``live`` is the queryset used to *select* a reference.
        self.assertTrue(
            Reference.objects.visible_to(self.admin).filter(pk=self.reference.pk).exists()
        )
        self.assertFalse(
            Reference.objects.live().filter(pk=self.reference.pk).exists()
        )

    def test_restore_brings_it_back(self):
        soft_delete_reference(self.reference)
        restore_reference(self.reference)
        self.reference.refresh_from_db()
        self.assertFalse(self.reference.is_deleted)
        self.assertTrue(self.reference.is_active)


class PrivateReferenceTests(TestCase):
    def setUp(self):
        self.inspector = make_inspector("insp-1")
        self.other = make_inspector("insp-2")
        self.admin = make_admin()

    def test_private_reference_requires_an_owner_at_the_database_level(self):
        reference = Reference(title="بلا مالك", visibility=ReferenceVisibility.PRIVATE)
        with self.assertRaises(Exception):
            reference.save()

    def test_private_reference_is_invisible_to_other_inspectors(self):
        reference = make_private_reference(self.inspector)
        visible_to_other = Reference.objects.visible_to(self.other)
        self.assertFalse(visible_to_other.filter(pk=reference.pk).exists())
        self.assertTrue(Reference.objects.visible_to(self.inspector).filter(pk=reference.pk).exists())

    def test_private_reference_never_becomes_shared_on_its_own(self):
        reference = make_private_reference(self.inspector)
        self.assertTrue(reference.is_private)
        self.assertIsNone(reference.approved_from_submission.first())


class SubmissionSnapshotTests(TestCase):
    """Approval creates a new shared reference; the private one is untouched."""

    def setUp(self):
        self.inspector = make_inspector("insp")
        self.admin = make_admin()
        self.reference = make_private_reference(self.inspector, "مرجعي الخاص")
        add_branch(self.reference, "P-1", "فرع خاص")
        add_item(self.reference, "P-1-A", "بند خاص",
                 parent=self.reference.nodes.get(code="P-1"))

    def test_submission_freezes_content_at_submission_time(self):
        submission = create_submission(reference=self.reference, actor=self.inspector)
        codes = [node["code"] for node in submission.snapshot["nodes"]]
        self.assertEqual(codes, ["P-1", "P-1-A"])

        # Editing the private reference afterwards must not change the snapshot.
        add_item(self.reference, "P-1-B", "بند أضيف لاحقًا",
                 parent=self.reference.nodes.get(code="P-1"))
        self.reference.refresh_from_db()
        self.assertNotIn("P-1-B", [n["code"] for n in submission.snapshot["nodes"]])

    def test_only_one_pending_submission_per_reference(self):
        create_submission(reference=self.reference, actor=self.inspector)
        with self.assertRaises(ValidationError):
            create_submission(reference=self.reference, actor=self.inspector)

    def test_shared_reference_cannot_be_submitted(self):
        shared = make_shared_reference(self.admin, "مشترك")
        with self.assertRaises(ValidationError):
            create_submission(reference=shared, actor=self.admin)

    def test_non_owner_cannot_submit(self):
        other = make_inspector("other")
        with self.assertRaises(ValidationError):
            create_submission(reference=self.reference, actor=other)

    def test_approval_creates_an_independent_shared_reference(self):
        submission = create_submission(reference=self.reference, actor=self.inspector)
        shared = approve_submission(submission=submission, actor=self.admin)

        self.assertTrue(shared.is_shared)
        self.assertNotEqual(shared.pk, self.reference.pk)
        self.assertEqual(shared.nodes.count(), 2)
        # The private reference keeps ownership and is not converted.
        self.reference.refresh_from_db()
        self.assertTrue(self.reference.is_private)
        self.assertEqual(self.reference.owner, self.inspector)

    def test_shared_reference_from_approval_is_editable_without_touching_private(self):
        submission = create_submission(reference=self.reference, actor=self.inspector)
        shared = approve_submission(submission=submission, actor=self.admin)
        add_item(shared, "NEW", "بند جديد", parent=shared.nodes.get(code="P-1"))
        self.assertEqual(self.reference.nodes.count(), 2)
        self.assertEqual(shared.nodes.count(), 3)

    def test_rejection_keeps_the_reference_private(self):
        submission = create_submission(reference=self.reference, actor=self.inspector)
        reject_submission(submission=submission, actor=self.admin, note="غير مناسب")
        submission.refresh_from_db()
        self.reference.refresh_from_db()
        self.assertEqual(submission.status, "REJECTED")
        self.assertTrue(self.reference.is_private)
        self.assertEqual(self.reference.owner, self.inspector)

    def test_submission_cannot_be_reviewed_twice(self):
        submission = create_submission(reference=self.reference, actor=self.inspector)
        reject_submission(submission=submission, actor=self.admin)
        with self.assertRaises(ValidationError):
            approve_submission(submission=submission, actor=self.admin)


class ReferenceViewPermissionTests(TestCase):
    def setUp(self):
        self.inspector = make_inspector("insp")
        self.other = make_inspector("other")
        self.admin = make_admin()
        self.reference = make_reference_tree(self.admin)

    def test_anonymous_is_redirected_from_the_library(self):
        response = self.client.get(reverse("catalog:list"))
        self.assertEqual(response.status_code, 302)

    def test_inspector_cannot_create_a_shared_reference(self):
        self.client.force_login(self.inspector)
        response = self.client.post(
            reverse("catalog:create_shared"), {"title": "محاولة", "description": ""}
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Reference.objects.filter(title="محاولة").exists())

    def test_inspector_cannot_edit_a_shared_reference(self):
        self.client.force_login(self.inspector)
        node = self.reference.nodes.get(code="B1")
        response = self.client.post(
            reverse("catalog:edit", args=[self.reference.pk]), {"title": "تغيير"}
        )
        self.assertEqual(response.status_code, 403)
        self.reference.refresh_from_db()
        self.assertNotEqual(self.reference.title, "تغيير")

    def test_admin_can_create_and_edit_a_shared_reference(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("catalog:create_shared"),
            {"title": "مرجع جديد", "description": "وصف"},
        )
        self.assertEqual(response.status_code, 302)
        created = Reference.objects.get(title="مرجع جديد")
        response = self.client.post(
            reverse("catalog:edit", args=[created.pk]),
            {"title": "مرجع محدّث", "description": "وصف"},
        )
        self.assertEqual(response.status_code, 302)
        created.refresh_from_db()
        self.assertEqual(created.title, "مرجع محدّث")

    def test_inspector_cannot_read_another_inspectors_private_reference(self):
        private = make_private_reference(self.other, "سري")
        self.client.force_login(self.inspector)
        response = self.client.get(reverse("catalog:detail", args=[private.pk]))
        self.assertEqual(response.status_code, 403)

    def test_admin_cannot_review_a_non_pending_submission(self):
        private = make_private_reference(self.inspector, "خاص")
        submission = create_submission(reference=private, actor=self.inspector)
        reject_submission(submission=submission, actor=self.admin)
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("catalog:submission_approve", args=[submission.pk]), {"note": ""}
        )
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertEqual(submission.status, "REJECTED")

    def test_admin_node_delete_does_not_ask_permission_from_visits(self):
        self.client.force_login(self.admin)
        node = self.reference.nodes.get(code="B1-D-2")
        response = self.client.post(
            reverse("catalog:node_delete", args=[self.reference.pk, node.pk])
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.reference.nodes.filter(code="B1-D-2").exists())