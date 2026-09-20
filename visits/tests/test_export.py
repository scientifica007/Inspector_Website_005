"""Export tests.

The export must be deterministic and complete: it is the artefact a reader uses
to understand a visit historically without the live reference, guide or
assignment rows.
"""

import json

from django.test import TestCase

from catalog.models import ValueType
from catalog.services import soft_delete_reference
from core.testing import (
    add_branch,
    add_description,
    make_admin,
    make_assignment,
    make_guide,
    make_inspector,
    make_reference_tree,
    make_shared_reference,
    make_visit,
)
from governance.services import (
    apply_guide,
    issue_assignment,
    revoke_assignment,
)
from visits.export import EXPORT_SCHEMA, EXPORT_VERSION, build_visit_export
from visits.models import ItemResult, Origin
from visits.services import (
    add_local_node,
    complete_visit,
    exclude_node,
    record_result,
    record_value,
    restore_node,
    select_node,
)


class ExportShapeTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.inspector = make_inspector()
        self.reference = make_reference_tree(self.admin)
        self.visit = make_visit(self.inspector, self.reference)

    def test_schema_and_version_are_present(self):
        payload = build_visit_export(self.visit)
        self.assertEqual(payload["schema"], EXPORT_SCHEMA)
        self.assertEqual(payload["version"], EXPORT_VERSION)

    def test_identity_fields(self):
        payload = build_visit_export(self.visit)
        self.assertEqual(payload["visit"]["inspector"], "inspector")
        self.assertEqual(payload["visit"]["status"], "DRAFT")
        self.assertEqual(
            payload["visit"]["institution"]["name_snapshot"],
            "معهد الاختبار",
        )
        self.assertEqual(
            payload["visit"]["source_reference"]["title_snapshot"],
            "مرجع التفتيش البيداغوجي",
        )

    def test_export_is_json_serialisable(self):
        json.dumps(build_visit_export(self.visit))

    def test_export_is_deterministic_for_an_unchanged_visit(self):
        first = json.dumps(build_visit_export(self.visit), ensure_ascii=False, sort_keys=True)
        second = json.dumps(build_visit_export(self.visit), ensure_ascii=False, sort_keys=True)
        self.assertEqual(first, second)

    def test_node_order_is_the_visible_tree_order(self):
        payload = build_visit_export(self.visit)
        codes = [node["code"] for node in payload["scope"]["nodes"]]
        self.assertEqual(
            codes, ["ROOT", "B1", "B1-D", "B1-D-1", "B1-D-2", "B1-1", "B2", "B2-1"]
        )

    def test_parent_code_and_depth_are_exported(self):
        payload = build_visit_export(self.visit)
        by_code = {node["code"]: node for node in payload["scope"]["nodes"]}
        self.assertIsNone(by_code["ROOT"]["parent_code"])
        self.assertEqual(by_code["B1-D-1"]["parent_code"], "B1-D")
        self.assertEqual(by_code["B1-D-1"]["depth"], 3)

    def test_no_reference_row_is_required(self):
        visit = make_visit(self.inspector)
        payload = build_visit_export(visit)
        self.assertIsNone(payload["visit"]["source_reference"]["title_snapshot"])
        self.assertEqual(payload["scope"]["nodes"], [])


class ExportScopeTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.inspector = make_inspector()
        self.reference = make_reference_tree(self.admin)
        self.visit = make_visit(self.inspector, self.reference)

    def node(self, code):
        return self.visit.nodes.get(code=code)

    def test_selected_and_context_roles_are_distinguished(self):
        select_node(visit=self.visit, node=self.node("B1-D-1"))
        payload = build_visit_export(self.visit)
        by_code = {node["code"]: node for node in payload["scope"]["nodes"]}
        self.assertEqual(by_code["B1-D-1"]["role"], "SELECTED")
        self.assertEqual(by_code["B1"]["role"], "CONTEXT")
        self.assertEqual(by_code["ROOT"]["role"], "CONTEXT")

    def test_origin_is_exported(self):
        select_node(visit=self.visit, node=self.node("B1-D-1"))
        payload = build_visit_export(self.visit)
        by_code = {node["code"]: node for node in payload["scope"]["nodes"]}
        self.assertEqual(by_code["B1-D-1"]["origin"], Origin.MANUAL)
        self.assertIsNone(by_code["B1"]["origin"])

    def test_excluded_status_and_via_are_exported(self):
        select_node(visit=self.visit, node=self.node("B1"))
        exclude_node(visit=self.visit, node=self.node("B1-D-1"))
        payload = build_visit_export(self.visit)
        by_code = {node["code"]: node for node in payload["scope"]["nodes"]}
        self.assertEqual(by_code["B1-D-1"]["status"], "EXCLUDED")
        self.assertIn("excluded_at", by_code["B1-D-1"])
        self.assertEqual(payload["scope"]["excluded_count"], 1)

    def test_restored_node_returns_to_active_with_data_intact(self):
        select_node(visit=self.visit, node=self.node("B1-D-1"))
        record_result(visit=self.visit, node=self.node("B1-D-1"), result=ItemResult.MATCH)
        exclude_node(visit=self.visit, node=self.node("B1-D-1"))
        restore_node(visit=self.visit, node=self.node("B1-D-1"))
        payload = build_visit_export(self.visit)
        by_code = {node["code"]: node for node in payload["scope"]["nodes"]}
        self.assertEqual(by_code["B1-D-1"]["status"], "ACTIVE")
        self.assertEqual(by_code["B1-D-1"]["result"], ItemResult.MATCH)

    def test_results_and_observations_are_exported(self):
        select_node(visit=self.visit, node=self.node("B1-D-1"))
        record_result(
            visit=self.visit,
            node=self.node("B1-D-1"),
            result=ItemResult.NOT_APPLICABLE,
            observation="لا ينطبق على هذا المسار",
        )
        payload = build_visit_export(self.visit)
        by_code = {node["code"]: node for node in payload["scope"]["nodes"]}
        self.assertEqual(by_code["B1-D-1"]["result"], ItemResult.NOT_APPLICABLE)
        self.assertEqual(
            by_code["B1-D-1"]["observation"], "لا ينطبق على هذا المسار"
        )

    def test_values_are_exported_on_descriptions(self):
        reference = make_shared_reference(self.admin, "مرجع بقيم")
        branch = add_branch(reference, "VB", "فرع بقيم")
        add_description(
            reference, "VB-TXT", "ملاحظة وصفية", parent=branch,
            value_type=ValueType.TEXT,
        )
        add_description(
            reference, "VB-NUM", "عدد", parent=branch,
            value_type=ValueType.NUMBER,
        )
        visit = make_visit(self.inspector, reference)
        text_node = visit.nodes.get(code="VB-TXT")
        number_node = visit.nodes.get(code="VB-NUM")
        select_node(visit=visit, node=text_node)
        select_node(visit=visit, node=number_node)
        record_value(visit=visit, node=text_node, value_text="نص وصفي")
        record_value(visit=visit, node=number_node, value_number=12)

        payload = build_visit_export(visit)
        by_code = {node["code"]: node for node in payload["scope"]["nodes"]}
        self.assertEqual(
            by_code["VB-TXT"]["value"], {"type": "TEXT", "value": "نص وصفي"}
        )
        self.assertEqual(
            by_code["VB-NUM"]["value"], {"type": "NUMBER", "value": "12"}
        )

    def test_unset_value_is_exported_as_null(self):
        reference = make_shared_reference(self.admin, "مرجع بقيم")
        branch = add_branch(reference, "VB", "فرع بقيم")
        add_description(
            reference, "VB-TXT", "ملاحظة وصفية", parent=branch,
            value_type=ValueType.TEXT,
        )
        visit = make_visit(self.inspector, reference)
        payload = build_visit_export(visit)
        by_code = {node["code"]: node for node in payload["scope"]["nodes"]}
        self.assertEqual(
            by_code["VB-TXT"]["value"], {"type": "TEXT", "value": None}
        )

    def test_plain_description_has_no_value_block(self):
        payload = build_visit_export(self.visit)
        by_code = {node["code"]: node for node in payload["scope"]["nodes"]}
        # B1-D has value_type NONE, so it carries no value.
        self.assertNotIn("value", by_code["B1-D"])

    def test_local_content_is_exported(self):
        add_local_node(visit=self.visit, node_type="BRANCH", title="عنصر محلي")
        payload = build_visit_export(self.visit)
        locals_ = [
            node for node in payload["scope"]["nodes"] if node["origin"] == Origin.LOCAL
        ]
        self.assertEqual(len(locals_), 1)
        self.assertTrue(locals_[0]["code"].startswith("LOCAL-"))

    def test_progress_block_matches_the_service(self):
        select_node(visit=self.visit, node=self.node("B1-D-1"))
        record_result(visit=self.visit, node=self.node("B1-D-1"), result=ItemResult.MATCH)
        payload = build_visit_export(self.visit)
        self.assertEqual(payload["progress"], {"done": 1, "total": 1, "percent": 100})


class ExportConstraintTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.inspector = make_inspector()
        self.reference = make_reference_tree(self.admin)
        self.visit = make_visit(self.inspector, self.reference)

    def node(self, code):
        return self.visit.nodes.get(code=code)

    def test_baseline_and_effective_constraints_are_separated(self):
        assignment = make_assignment(
            self.admin, self.visit, title="تكليف",
            entries=[("B1-D-1", True, True)],
        )
        issue_assignment(assignment=assignment, actor=self.admin)
        payload = build_visit_export(self.visit)

        baseline = payload["constraints"]["baseline"]
        effective = payload["constraints"]["effective"]
        # B1-D-2 is mandatory in the reference: baseline, not assignment.
        self.assertIn("B1-D-2", baseline["completion_required"])
        self.assertNotIn("B1-D-1", baseline["completion_required"])
        self.assertIn("B1-D-1", effective["completion_required"])
        self.assertIn("B1-D-1", effective["scope_locked"])

    def test_assignment_audit_history_is_exported(self):
        assignment = make_assignment(
            self.admin, self.visit, title="تكليف رسمي",
            entries=[("B1-D-1", True, False)],
        )
        issue_assignment(assignment=assignment, actor=self.admin)
        payload = build_visit_export(self.visit)
        self.assertEqual(payload["assignment_audit"]["issued_count"], 1)
        record = payload["assignments"][0]
        self.assertEqual(record["title"], "تكليف رسمي")
        self.assertEqual(record["status"], "ISSUED")
        self.assertEqual(record["issued_by"], "admin")
        self.assertIsNotNone(record["issued_at"])
        self.assertEqual(record["entries"][0]["target_code"], "B1-D-1")
        self.assertIs(record["entries"][0]["scope_locked"], True)

    def test_revoked_assignment_and_reason_are_exported(self):
        assignment = make_assignment(
            self.admin, self.visit, title="تكليف ملغى",
            entries=[("B1-D-1", True, False)],
        )
        issue_assignment(assignment=assignment, actor=self.admin)
        revoke_assignment(assignment=assignment, actor=self.admin, reason="خطأ إداري")
        payload = build_visit_export(self.visit)
        record = payload["assignments"][0]
        self.assertEqual(record["status"], "REVOKED")
        self.assertEqual(record["revoke_reason"], "خطأ إداري")
        self.assertEqual(record["revoked_by"], "admin")
        self.assertEqual(payload["assignment_audit"]["revoked_count"], 1)
        # The revoked obligation is gone from the effective set.
        self.assertNotIn("B1-D-1", payload["constraints"]["effective"]["scope_locked"])

    def test_guide_application_is_exported_with_its_snapshot(self):
        guide = make_guide(self.admin, self.reference, title="دليل", codes=["B1-D-1"])
        apply_guide(visit=self.visit, guide=guide, actor=self.inspector)
        payload = build_visit_export(self.visit)
        record = payload["guide_applications"][0]
        self.assertEqual(record["guide_title"], "دليل")
        self.assertEqual(record["guide_version"], 1)
        self.assertIs(record["guide_still_exists"], True)
        self.assertEqual(record["applied_by"], "inspector")

    def test_guide_deletion_is_reflected_without_losing_history(self):
        guide = make_guide(self.admin, self.reference, title="دليل", codes=["B1-D-1"])
        apply_guide(visit=self.visit, guide=guide, actor=self.inspector)
        guide.delete()
        payload = build_visit_export(self.visit)
        record = payload["guide_applications"][0]
        self.assertEqual(record["guide_title"], "دليل")
        self.assertIs(record["guide_still_exists"], False)


class ExportHistoricalTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.inspector = make_inspector()
        self.reference = make_reference_tree(self.admin)
        self.visit = make_visit(self.inspector, self.reference)

    def test_deleted_reference_is_still_described(self):
        title = self.reference.title
        soft_delete_reference(self.reference)
        self.visit.refresh_from_db()
        payload = build_visit_export(self.visit)
        self.assertEqual(payload["visit"]["source_reference"]["title_snapshot"], title)
        self.assertIs(payload["visit"]["source_reference"]["still_exists"], True)

    def test_current_revision_is_reported_separately_from_the_snapshot(self):
        snapshot_revision = self.visit.source_reference_revision
        add_branch(self.reference, "LATER", "فرع لاحق")
        self.visit.refresh_from_db()
        payload = build_visit_export(self.visit)
        self.assertEqual(
            payload["visit"]["source_reference"]["revision_snapshot"], snapshot_revision
        )
        self.assertGreater(
            payload["visit"]["source_reference"]["current_revision"], snapshot_revision
        )

    def test_completed_visit_export_is_stable(self):
        node = self.visit.nodes.get(code="B1-D-2")
        select_node(visit=self.visit, node=node)
        record_result(visit=self.visit, node=node, result=ItemResult.MATCH)
        complete_visit(visit=self.visit, actor=self.inspector)
        self.visit.refresh_from_db()

        first = json.dumps(build_visit_export(self.visit), ensure_ascii=False, sort_keys=True)
        # Mutating the live reference afterwards must not alter the export.
        self.reference.nodes.get(code="B1-D-2").title = "تغيير متأخر"
        self.reference.nodes.get(code="B1-D-2").save()
        second = json.dumps(build_visit_export(self.visit), ensure_ascii=False, sort_keys=True)
        self.assertEqual(first, second)

    def test_export_does_not_report_a_wall_clock_now(self):
        payload = build_visit_export(self.visit)
        # Only stored historical facts appear; a DRAFT has no completion time.
        self.assertIsNone(payload["visit"]["completed_at"])
        self.assertIsNone(payload["visit"]["completed_by"])