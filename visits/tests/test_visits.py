"""Visit lifecycle tests.

Covers the frozen snapshot, selective scope, context ancestors, soft exclusion,
local authoring, execution recording, progress, completion guarding and the
immutability of completed visits.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from catalog.models import ValueType
from catalog.services import soft_delete_reference
from core.templatetags.ui import value_display
from core.testing import (
    add_branch,
    add_description,
    add_item,
    make_admin,
    make_institution,
    make_inspector,
    make_reference_tree,
    make_shared_reference,
    make_visit,
)
from governance.models import Assignment, AssignmentStatus
from visits.models import ItemResult, NodeStatus, Origin, VisitStatus
from visits.services import (
    add_local_node,
    complete_visit,
    compute_progress,
    context_ancestors,
    create_visit,
    delete_draft,
    deselect_node,
    exclude_node,
    outstanding_requirements,
    record_result,
    record_value,
    restore_node,
    select_node,
)


class VisitCreationTests(TestCase):
    def setUp(self):
        self.inspector = make_inspector()
        self.institution = make_institution()
        self.reference = make_reference_tree(self.inspector)

    def test_visit_starts_as_draft(self):
        visit = make_visit(self.inspector, self.reference, self.institution)
        self.assertEqual(visit.status, VisitStatus.DRAFT)
        self.assertTrue(visit.is_draft)
        self.assertIsNone(visit.completed_at)

    def test_visit_can_be_created_without_a_reference(self):
        visit = create_visit(
            actor=self.inspector,
            institution=self.institution,
            visit_date="2026-02-01",
        )
        self.assertIsNone(visit.source_reference)
        self.assertEqual(visit.nodes.count(), 0)
        self.assertEqual(visit.source_reference_label, "بدون مرجع")

    def test_snapshot_copies_the_whole_tree(self):
        visit = make_visit(self.inspector, self.reference, self.institution)
        self.assertEqual(visit.nodes.count(), self.reference.nodes.count())
        self.assertEqual(
            sorted(visit.nodes.values_list("code", flat=True)),
            sorted(self.reference.nodes.values_list("code", flat=True)),
        )

    def test_scope_starts_empty_even_though_the_snapshot_is_full(self):
        visit = make_visit(self.inspector, self.reference, self.institution)
        self.assertEqual(visit.selected_nodes().count(), 0)
        # Snapshot nodes exist as available structure and carry no origin until
        # they actually enter the scope.
        self.assertTrue(all(node.origin is None for node in visit.nodes.all()))
        self.assertEqual(visit.nodes.count(), self.reference.nodes.count())

    def test_all_snapshot_nodes_start_active(self):
        visit = make_visit(self.inspector, self.reference, self.institution)
        self.assertEqual(
            visit.nodes.filter(status=NodeStatus.EXCLUDED).count(), 0
        )

    def test_provenance_is_recorded(self):
        visit = make_visit(self.inspector, self.reference, self.institution)
        self.assertEqual(visit.source_reference_title, self.reference.title)
        self.assertEqual(visit.source_reference_visibility, self.reference.visibility)
        self.assertEqual(
            visit.source_reference_revision, self.reference.revision
        )
        self.assertEqual(visit.institution_name_snapshot, self.institution.name)

    def test_inspector_cannot_create_a_visit_from_someone_elses_private_reference(self):
        other = make_inspector("other")
        private = make_shared_reference(other, "خاصة")
        private.visibility = "PRIVATE"
        private.owner = other
        private.save()
        with self.assertRaises(ValidationError):
            create_visit(
                actor=self.inspector,
                institution=self.institution,
                visit_date="2026-01-01",
                reference=private,
            )


class FrozenSnapshotTests(TestCase):
    """Historical truth is protected against later reference changes."""

    def setUp(self):
        self.inspector = make_inspector()
        self.admin = make_admin()
        self.reference = make_reference_tree(self.admin)
        self.visit = make_visit(self.inspector, self.reference)
        select_node(
            visit=self.visit, node=self.visit.nodes.get(code="B1-D-1")
        )

    def test_editing_the_reference_after_creation_does_not_change_the_visit(self):
        node = self.reference.nodes.get(code="B1-D-1")
        node.title = "عنوان مُغيَّر في المرجع"
        node.save()
        self.visit.nodes.get(code="B1-D-1").refresh_from_db()
        self.assertNotEqual(
            self.visit.nodes.get(code="B1-D-1").title, "عنوان مُغيَّر في المرجع"
        )

    def test_adding_to_the_reference_after_creation_does_not_appear_in_the_visit(self):
        add_item(self.reference, "NEW-1", "بند جديد",
                 parent=self.reference.nodes.get(code="B1"))
        self.assertFalse(self.visit.nodes.filter(code="NEW-1").exists())

    def test_deleting_a_reference_node_does_not_touch_the_visit(self):
        before = self.visit.nodes.count()
        self.reference.nodes.get(code="B1-D-2").delete()
        self.assertEqual(self.visit.nodes.count(), before)
        self.assertTrue(self.visit.nodes.filter(code="B1-D-2").exists())

    def test_deleting_the_whole_reference_does_not_touch_the_visit(self):
        soft_delete_reference(self.reference)
        self.visit.refresh_from_db()
        self.assertEqual(self.visit.nodes.count(), self.reference.nodes.count() + 0)
        self.assertIn("محذوف", self.visit.source_reference_label)

    def test_visit_keeps_the_reference_title_after_deletion(self):
        title = self.reference.title
        soft_delete_reference(self.reference)
        self.visit.refresh_from_db()
        self.assertEqual(self.visit.source_reference_title, title)

    def test_snapshot_does_not_read_from_the_live_reference(self):
        # Changing the live node must never alter the frozen node, even in body.
        live = self.reference.nodes.get(code="B1-D")
        live.body = "نص جديد"
        live.value_type = ValueType.TEXT
        live.save()
        frozen = self.visit.nodes.get(code="B1-D")
        frozen.refresh_from_db()
        self.assertNotEqual(frozen.body, "نص جديد")
        self.assertEqual(frozen.value_type, ValueType.NONE)


class SelectiveScopeTests(TestCase):
    def setUp(self):
        self.inspector = make_inspector()
        self.reference = make_reference_tree(self.inspector)
        self.visit = make_visit(self.inspector, self.reference)

    def node(self, code):
        return self.visit.nodes.get(code=code)

    def test_selecting_a_deep_item_marks_only_it_for_progress(self):
        select_node(visit=self.visit, node=self.node("B1-D-1"))
        selected = set(
            self.visit.nodes.filter(is_selected=True).values_list("code", flat=True)
        )
        self.assertEqual(selected, {"B1-D-1"})

    def test_context_ancestors_are_available_but_not_selected(self):
        node = self.node("B1-D-1")
        ancestors = context_ancestors(node)
        self.assertEqual([a.code for a in ancestors], ["ROOT", "B1", "B1-D"])
        for ancestor in ancestors:
            ancestor.refresh_from_db()
            self.assertFalse(ancestor.is_selected)

    def test_context_nodes_do_not_count_towards_progress(self):
        select_node(visit=self.visit, node=self.node("B1-D-1"))
        progress = compute_progress(self.visit)
        self.assertEqual(progress.total, 1)

    def test_selecting_a_branch_selects_its_subtree(self):
        select_node(visit=self.visit, node=self.node("B1"))
        selected = set(
            self.visit.nodes.filter(is_selected=True).values_list("code", flat=True)
        )
        self.assertEqual(selected, {"B1", "B1-D", "B1-D-1", "B1-D-2", "B1-1"})

    def test_origin_is_manual_for_a_manual_selection(self):
        select_node(visit=self.visit, node=self.node("B1-D-1"))
        self.assertEqual(self.node("B1-D-1").origin, Origin.MANUAL)

    def test_context_ancestors_keep_no_origin_until_selected(self):
        select_node(visit=self.visit, node=self.node("B1-D-1"))
        self.assertIsNone(self.node("B1").origin)
        self.assertIsNone(self.node("ROOT").origin)

    def test_deselecting_keeps_the_node_in_the_snapshot(self):
        select_node(visit=self.visit, node=self.node("B1-D-1"))
        deselect_node(visit=self.visit, node=self.node("B1-D-1"))
        self.assertTrue(self.visit.nodes.filter(code="B1-D-1").exists())
        self.assertFalse(self.node("B1-D-1").is_selected)

    def test_deselecting_does_not_erase_the_origin(self):
        select_node(visit=self.visit, node=self.node("B1-D-1"))
        deselect_node(visit=self.visit, node=self.node("B1-D-1"))
        self.assertEqual(self.node("B1-D-1").origin, Origin.MANUAL)

    def test_deselecting_a_branch_deselects_its_subtree(self):
        select_node(visit=self.visit, node=self.node("B1"))
        deselect_node(visit=self.visit, node=self.node("B1"))
        self.assertEqual(self.visit.nodes.filter(is_selected=True).count(), 0)


class ExclusionTests(TestCase):
    """Soft exclusion preserves every recorded value."""

    def setUp(self):
        self.inspector = make_inspector()
        self.reference = make_reference_tree(self.inspector)
        self.visit = make_visit(self.inspector, self.reference)
        select_node(visit=self.visit, node=self.visit.nodes.get(code="B1-D-1"))
        record_result(
            visit=self.visit,
            node=self.visit.nodes.get(code="B1-D-1"),
            result=ItemResult.NOT_MATCH,
            observation="ملاحظة مهمة",
        )

    def test_exclusion_keeps_the_result_and_observation(self):
        node = self.visit.nodes.get(code="B1-D-1")
        exclude_node(visit=self.visit, node=node)
        node.refresh_from_db()
        self.assertTrue(node.is_excluded)
        self.assertEqual(node.result, ItemResult.NOT_MATCH)
        self.assertEqual(node.observation, "ملاحظة مهمة")

    def test_excluded_node_leaves_the_scope_and_progress(self):
        exclude_node(visit=self.visit, node=self.visit.nodes.get(code="B1-D-1"))
        progress = compute_progress(self.visit)
        self.assertEqual(progress.total, 0)
        self.assertEqual(progress.done, 0)

    def test_restore_brings_back_the_same_data(self):
        node = self.visit.nodes.get(code="B1-D-1")
        exclude_node(visit=self.visit, node=node)
        restore_node(visit=self.visit, node=node)
        node.refresh_from_db()
        self.assertTrue(node.is_active)
        self.assertTrue(node.is_selected)
        self.assertEqual(node.result, ItemResult.NOT_MATCH)
        self.assertEqual(node.observation, "ملاحظة مهمة")

    def test_restore_puts_it_back_into_progress(self):
        node = self.visit.nodes.get(code="B1-D-1")
        exclude_node(visit=self.visit, node=node)
        restore_node(visit=self.visit, node=node)
        progress = compute_progress(self.visit)
        self.assertEqual(progress.done, 1)
        self.assertEqual(progress.total, 1)

    def test_excluding_a_branch_excludes_descendants(self):
        select_node(visit=self.visit, node=self.visit.nodes.get(code="B1"))
        exclude_node(visit=self.visit, node=self.visit.nodes.get(code="B1"))
        for code in ["B1", "B1-D", "B1-D-1", "B1-D-2", "B1-1"]:
            self.assertTrue(self.visit.nodes.get(code=code).is_excluded)

    def test_restoring_a_branch_restores_descendants_excluded_through_it(self):
        select_node(visit=self.visit, node=self.visit.nodes.get(code="B1"))
        exclude_node(visit=self.visit, node=self.visit.nodes.get(code="B1"))
        restore_node(visit=self.visit, node=self.visit.nodes.get(code="B1"))
        for code in ["B1", "B1-D", "B1-D-1", "B1-D-2", "B1-1"]:
            self.assertTrue(self.visit.nodes.get(code=code).is_active)

    def test_restoring_a_branch_does_not_revive_a_descendant_excluded_separately(self):
        select_node(visit=self.visit, node=self.visit.nodes.get(code="B1"))
        deep = self.visit.nodes.get(code="B1-D-1")
        exclude_node(visit=self.visit, node=deep)          # explicit exclusion first
        exclude_node(visit=self.visit, node=self.visit.nodes.get(code="B1"))
        deep.refresh_from_db()
        self.assertIsNone(deep.excluded_via_id)

        restore_node(visit=self.visit, node=self.visit.nodes.get(code="B1"))
        # B1 and its other descendants come back; the explicitly excluded B1-D-1
        # stays excluded, because restoring must not override a decision.
        self.assertTrue(self.visit.nodes.get(code="B1").is_active)
        deep.refresh_from_db()
        self.assertTrue(deep.is_excluded)


class LocalContentTests(TestCase):
    def setUp(self):
        self.inspector = make_inspector()
        self.visit = make_visit(self.inspector)

    def test_local_branch_is_scoped_to_the_visit_only(self):
        node = add_local_node(
            visit=self.visit, node_type="BRANCH", title="فرع محلي"
        )
        self.assertEqual(node.origin, Origin.LOCAL)
        self.assertTrue(node.is_selected)
        self.assertTrue(node.code.startswith("LOCAL-"))
        self.assertEqual(self.visit.nodes.count(), 1)

    def test_local_content_is_not_pushed_to_governance(self):
        add_local_node(visit=self.visit, node_type="BRANCH", title="فرع محلي")
        self.assertEqual(Assignment.objects.filter(visit=self.visit).count(), 0)

    def test_local_children_are_selected_with_their_parent(self):
        parent = add_local_node(
            visit=self.visit, node_type="BRANCH", title="فرع محلي"
        )
        child = add_local_node(
            visit=self.visit, node_type="CHECKLIST_ITEM", title="بند محلي",
            parent=parent,
        )
        self.assertTrue(child.is_selected)
        progress = compute_progress(self.visit)
        self.assertEqual(progress.total, 1)


class ExecutionTests(TestCase):
    def setUp(self):
        self.inspector = make_inspector()
        self.reference = make_reference_tree(self.inspector)
        self.visit = make_visit(self.inspector, self.reference)

    def node(self, code):
        return self.visit.nodes.get(code=code)

    def test_result_is_recorded_on_a_checklist_item(self):
        select_node(visit=self.visit, node=self.node("B1-D-1"))
        record_result(
            visit=self.visit, node=self.node("B1-D-1"), result=ItemResult.MATCH,
            observation="ملاحظة",
        )
        node = self.node("B1-D-1")
        node.refresh_from_db()
        self.assertEqual(node.result, ItemResult.MATCH)
        self.assertEqual(node.observation, "ملاحظة")

    def test_not_applicable_is_distinct_from_not_match(self):
        select_node(visit=self.visit, node=self.node("B1-D-1"))
        record_result(
            visit=self.visit, node=self.node("B1-D-1"), result=ItemResult.NOT_APPLICABLE
        )
        self.assertEqual(self.node("B1-D-1").result, ItemResult.NOT_APPLICABLE)
        self.assertNotEqual(
            self.node("B1-D-1").result, ItemResult.NOT_MATCH
        )

    def test_result_cannot_be_recorded_on_a_branch(self):
        with self.assertRaises(ValidationError):
            record_result(
                visit=self.visit, node=self.node("B1"), result=ItemResult.MATCH
            )

    def test_result_cannot_be_recorded_on_a_plain_description(self):
        with self.assertRaises(ValidationError):
            record_result(
                visit=self.visit, node=self.node("B1-D"), result=ItemResult.MATCH
            )

    def test_unknown_result_is_rejected(self):
        with self.assertRaises(ValidationError):
            record_result(
                visit=self.visit, node=self.node("B1-D-1"), result="MAYBE"
            )

    def test_value_is_recorded_on_a_value_description(self):
        reference = make_shared_reference(self.inspector, "مرجع بالقيم")
        branch = add_branch(reference, "V-B", "فرع")
        add_description(
            reference, "V-N", "عدد المتدربين", parent=branch,
            value_type=ValueType.NUMBER,
        )
        visit = make_visit(self.inspector, reference)
        node = visit.nodes.get(code="V-N")
        select_node(visit=visit, node=node)
        record_value(visit=visit, node=node, value_number=42)
        node.refresh_from_db()
        self.assertEqual(node.value_number, Decimal("42"))
        self.assertTrue(node.is_fulfilled)
        # Renderable as a clean integer, not "42.000".
        self.assertEqual(value_display(node), "42")

    def test_value_cannot_be_recorded_on_a_checklist_item(self):
        with self.assertRaises(ValidationError):
            record_value(visit=self.visit, node=self.node("B1-D-1"), value_text="x")

    def test_recording_on_a_completed_visit_is_refused(self):
        select_item = self.node("B1-D-1")
        select_node(visit=self.visit, node=select_item)
        record_result(visit=self.visit, node=select_item, result=ItemResult.MATCH)
        mandatory = self.node("B1-D-2")
        select_node(visit=self.visit, node=mandatory)
        record_result(visit=self.visit, node=mandatory, result=ItemResult.MATCH)
        self.visit.refresh_from_db()
        complete_visit(visit=self.visit, actor=self.inspector)
        self.visit.refresh_from_db()
        with self.assertRaises(ValidationError):
            record_result(
                visit=self.visit, node=select_item, result=ItemResult.NOT_MATCH
            )


class ProgressTests(TestCase):
    def setUp(self):
        self.inspector = make_inspector()
        self.reference = make_reference_tree(self.inspector)
        self.visit = make_visit(self.inspector, self.reference)

    def node(self, code):
        return self.visit.nodes.get(code=code)

    def test_progress_counts_only_selected_completable_nodes(self):
        select_node(visit=self.visit, node=self.node("B1-D-1"))
        select_node(visit=self.visit, node=self.node("B1-D-2"))
        progress = compute_progress(self.visit)
        self.assertEqual(progress.total, 2)
        self.assertEqual(progress.done, 0)
        self.assertEqual(progress.percent, 0)

    def test_progress_reaches_full_percent(self):
        select_node(visit=self.visit, node=self.node("B1-D-1"))
        record_result(visit=self.visit, node=self.node("B1-D-1"), result=ItemResult.MATCH)
        progress = compute_progress(self.visit)
        self.assertEqual(progress.done, 1)
        self.assertEqual(progress.percent, 100)

    def test_empty_scope_reports_one_hundred_percent(self):
        progress = compute_progress(self.visit)
        self.assertEqual((progress.done, progress.total), (0, 0))
        self.assertEqual(progress.percent, 100)

    def test_excluded_nodes_are_not_counted(self):
        select_node(visit=self.visit, node=self.node("B1-D-1"))
        select_node(visit=self.visit, node=self.node("B1-D-2"))
        exclude_node(visit=self.visit, node=self.node("B1-D-1"))
        self.assertEqual(compute_progress(self.visit).total, 1)


class CompletionTests(TestCase):
    def setUp(self):
        self.inspector = make_inspector()
        self.reference = make_reference_tree(self.inspector)
        self.visit = make_visit(self.inspector, self.reference)

    def node(self, code):
        return self.visit.nodes.get(code=code)

    def test_mandatory_item_blocks_completion(self):
        select_node(visit=self.visit, node=self.node("B1-D-1"))
        select_node(visit=self.visit, node=self.node("B1-D-2"))  # mandatory
        record_result(visit=self.visit, node=self.node("B1-D-1"), result=ItemResult.MATCH)
        with self.assertRaises(ValidationError):
            complete_visit(visit=self.visit, actor=self.inspector)

    def test_completion_succeeds_once_requirements_are_met(self):
        select_node(visit=self.visit, node=self.node("B1-D-2"))
        record_result(visit=self.visit, node=self.node("B1-D-2"), result=ItemResult.MATCH)
        complete_visit(visit=self.visit, actor=self.inspector)
        self.visit.refresh_from_db()
        self.assertEqual(self.visit.status, VisitStatus.COMPLETED)
        self.assertIsNotNone(self.visit.completed_at)
        self.assertEqual(self.visit.completed_by, self.inspector)

    def test_excluded_mandatory_item_no_longer_blocks_completion(self):
        select_node(visit=self.visit, node=self.node("B1-D-2"))
        exclude_node(visit=self.visit, node=self.node("B1-D-2"))
        complete_visit(visit=self.visit, actor=self.inspector)
        self.visit.refresh_from_db()
        self.assertTrue(self.visit.is_completed)

    def test_outstanding_requirements_lists_unfulfilled_mandatory_items_only(self):
        select_node(visit=self.visit, node=self.node("B1-D-1"))
        select_node(visit=self.visit, node=self.node("B1-D-2"))
        outstanding = outstanding_requirements(self.visit)
        self.assertEqual([n.code for n in outstanding], ["B1-D-2"])


class CompletedImmutabilityTests(TestCase):
    def setUp(self):
        self.inspector = make_inspector()
        self.admin = make_admin()
        self.reference = make_reference_tree(self.admin)
        self.visit = make_visit(self.inspector, self.reference)
        select_node(visit=self.visit, node=self.visit.nodes.get(code="B1-D-1"))
        select_node(visit=self.visit, node=self.visit.nodes.get(code="B1-D-2"))
        record_result(
            visit=self.visit,
            node=self.visit.nodes.get(code="B1-D-1"),
            result=ItemResult.MATCH,
            observation="ملاحظة أصلية",
        )
        record_result(
            visit=self.visit,
            node=self.visit.nodes.get(code="B1-D-2"),
            result=ItemResult.MATCH,
        )
        complete_visit(visit=self.visit, actor=self.inspector)
        self.visit.refresh_from_db()

    def test_result_cannot_change(self):
        with self.assertRaises(ValidationError):
            record_result(
                visit=self.visit,
                node=self.visit.nodes.get(code="B1-D-1"),
                result=ItemResult.NOT_MATCH,
            )

    def test_scope_cannot_change(self):
        with self.assertRaises(ValidationError):
            select_node(visit=self.visit, node=self.visit.nodes.get(code="B1-D-2"))

    def test_exclusion_cannot_change(self):
        with self.assertRaises(ValidationError):
            exclude_node(visit=self.visit, node=self.visit.nodes.get(code="B1-D-1"))

    def test_restore_cannot_change(self):
        with self.assertRaises(ValidationError):
            restore_node(visit=self.visit, node=self.visit.nodes.get(code="B1-D-1"))

    def test_local_authoring_is_refused(self):
        with self.assertRaises(ValidationError):
            add_local_node(visit=self.visit, node_type="BRANCH", title="جديد")

    def test_completion_cannot_be_reapplied(self):
        with self.assertRaises(ValidationError):
            complete_visit(visit=self.visit, actor=self.inspector)

    def test_deletion_is_refused(self):
        with self.assertRaises(ValidationError):
            delete_draft(visit=self.visit, actor=self.inspector)
        self.assertTrue(type(self.visit).objects.filter(pk=self.visit.pk).exists())

    def test_editing_the_reference_does_not_rewrite_the_completed_visit(self):
        node = self.reference.nodes.get(code="B1-D-1")
        node.title = "تغيير بعد الإكمال"
        node.save()
        self.visit.nodes.get(code="B1-D-1").refresh_from_db()
        self.assertEqual(self.visit.nodes.get(code="B1-D-1").title, "بند أول")

    def test_deleting_the_reference_does_not_rewrite_the_completed_visit(self):
        soft_delete_reference(self.reference)
        self.visit.refresh_from_db()
        self.assertEqual(self.visit.nodes.get(code="B1-D-1").result, ItemResult.MATCH)
        self.assertEqual(
            self.visit.nodes.get(code="B1-D-1").observation, "ملاحظة أصلية"
        )


class DraftDeletionTests(TestCase):
    def setUp(self):
        self.inspector = make_inspector()
        self.other = make_inspector("other")
        self.admin = make_admin()
        self.visit = make_visit(self.inspector)

    def test_owner_can_delete_their_draft(self):
        pk = self.visit.pk
        delete_draft(visit=self.visit, actor=self.inspector)
        self.assertFalse(type(self.visit).objects.filter(pk=pk).exists())

    def test_another_inspector_cannot_delete_the_draft(self):
        with self.assertRaises(ValidationError):
            delete_draft(visit=self.visit, actor=self.other)

    def test_administrator_cannot_delete_another_inspectors_draft(self):
        # Documented policy: the draft is the inspector's own professional work.
        with self.assertRaises(ValidationError):
            delete_draft(visit=self.visit, actor=self.admin)

    def test_completed_visit_cannot_be_deleted_by_owner(self):
        reference = make_reference_tree(self.admin)
        visit = make_visit(self.inspector, reference)
        node = visit.nodes.get(code="B1-D-2")
        select_node(visit=visit, node=node)
        record_result(visit=visit, node=node, result=ItemResult.MATCH)
        complete_visit(visit=visit, actor=self.inspector)
        visit.refresh_from_db()
        with self.assertRaises(ValidationError):
            delete_draft(visit=visit, actor=self.inspector)

    def test_draft_with_an_issued_assignment_cannot_be_deleted(self):
        visit = make_visit(self.inspector, make_reference_tree(self.admin))
        Assignment.objects.create(
            visit=visit, title="تكليف", created_by=self.admin,
            status=AssignmentStatus.ISSUED,
            issued_by=self.admin, issued_at=timezone.now(),
        )
        with self.assertRaises(ValidationError):
            delete_draft(visit=visit, actor=self.inspector)


class VisitViewTests(TestCase):
    def setUp(self):
        self.inspector = make_inspector()
        self.other = make_inspector("other")
        self.admin = make_admin()
        self.institution = make_institution()
        self.reference = make_reference_tree(self.admin)
        self.visit = make_visit(self.inspector, self.reference, self.institution)

    def test_anonymous_redirected(self):
        response = self.client.get(reverse("visits:list"))
        self.assertEqual(response.status_code, 302)

    def test_inspector_list_shows_only_their_visits(self):
        make_visit(self.other, None, self.institution)
        self.client.force_login(self.inspector)
        response = self.client.get(reverse("visits:list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["visits"]), [self.visit])

    def test_inspector_cannot_open_another_inspectors_visit(self):
        self.client.force_login(self.other)
        response = self.client.get(reverse("visits:detail", args=[self.visit.pk]))
        self.assertEqual(response.status_code, 403)

    def test_admin_can_read_a_visit_but_cannot_edit_it(self):
        self.client.force_login(self.admin)
        self.assertEqual(
            self.client.get(reverse("visits:detail", args=[self.visit.pk])).status_code,
            200,
        )
        response = self.client.post(
            reverse("visits:edit", args=[self.visit.pk]), {"title": "تعديل", "notes": ""}
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_cannot_create_a_visit(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("visits:create"))
        self.assertEqual(response.status_code, 403)

    def test_other_inspector_cannot_delete_the_draft_via_http(self):
        self.client.force_login(self.other)
        response = self.client.post(reverse("visits:delete", args=[self.visit.pk]))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(type(self.visit).objects.filter(pk=self.visit.pk).exists())

    def test_scope_selection_via_http(self):
        self.client.force_login(self.inspector)
        node = self.visit.nodes.get(code="B1-D-1")
        response = self.client.post(
            reverse("visits:scope_select", args=[self.visit.pk, node.pk])
        )
        self.assertEqual(response.status_code, 302)
        node.refresh_from_db()
        self.assertTrue(node.is_selected)

    def test_scope_exclusion_of_a_locked_node_is_refused_on_the_server(self):
        from core.testing import make_assignment
        from governance.services import issue_assignment

        node = self.visit.nodes.get(code="B1-D-1")
        select_node(visit=self.visit, node=node)
        assignment = make_assignment(self.admin, self.visit, entries=[("B1-D-1", True, False)])
        issue_assignment(assignment=assignment, actor=self.admin)

        self.client.force_login(self.inspector)
        response = self.client.post(
            reverse("visits:scope_exclude", args=[self.visit.pk, node.pk])
        )
        self.assertEqual(response.status_code, 302)
        node.refresh_from_db()
        self.assertFalse(node.is_excluded)
        self.assertTrue(node.is_selected)

    def test_completion_via_http_blocks_on_requirements(self):
        self.client.force_login(self.inspector)
        node = self.visit.nodes.get(code="B1-D-2")
        select_node(visit=self.visit, node=node)
        response = self.client.post(reverse("visits:complete", args=[self.visit.pk]))
        self.assertEqual(response.status_code, 302)
        self.visit.refresh_from_db()
        self.assertTrue(self.visit.is_draft)

    def test_cannot_execute_an_owned_visit_when_it_is_completed(self):
        node = self.visit.nodes.get(code="B1-D-2")
        select_node(visit=self.visit, node=node)
        record_result(visit=self.visit, node=node, result=ItemResult.MATCH)
        complete_visit(visit=self.visit, actor=self.inspector)
        self.client.force_login(self.inspector)
        response = self.client.get(reverse("visits:execute", args=[self.visit.pk]))
        self.assertEqual(response.status_code, 403)

    def test_export_endpoint_returns_versioned_json(self):
        self.client.force_login(self.inspector)
        response = self.client.get(reverse("visits:export", args=[self.visit.pk]))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["schema"], "inspector.visit.export")
        self.assertGreaterEqual(payload["version"], 1)
        self.assertEqual(payload["visit"]["inspector"], self.inspector.username)

    def test_other_inspector_cannot_export(self):
        self.client.force_login(self.other)
        response = self.client.get(reverse("visits:export", args=[self.visit.pk]))
        self.assertEqual(response.status_code, 403)