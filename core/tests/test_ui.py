"""UI contract tests: Arabic, RTL, day-first dates and mobile-safe markup.

These are not pixel tests. They lock the properties the Arabic RTL product
depends on: the document direction, the date rendering helpers, and the absence
of hard-coded business data in templates.
"""

import re

from django.template import Context, Template
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.templatetags.ui import ardate, ardatetime
from core.testing import make_admin, make_inspector, make_reference_tree, make_visit
from visits.services import create_visit


class DateFilterTests(TestCase):
    def test_date_renders_day_first(self):
        import datetime

        self.assertEqual(ardate(datetime.date(2026, 3, 7)), "07/03/2026")

    def test_iso_string_is_rendered_verbatim(self):
        self.assertEqual(ardate("2026-03-07"), "2026-03-07")

    def test_empty_value_renders_a_dash(self):
        self.assertEqual(ardate(None), "—")
        self.assertEqual(ardatetime(None), "—")

    def test_datetime_renders_day_first_with_time(self):
        moment = timezone.make_aware(
            timezone.datetime(2026, 3, 7, 14, 5)
        )
        self.assertEqual(ardatetime(moment), "07/03/2026 14:05")

    def test_no_us_month_first_format_is_ever_produced(self):
        import datetime

        rendered = ardate(datetime.date(2026, 12, 1))
        self.assertEqual(rendered, "01/12/2026")
        self.assertNotEqual(rendered, "12/01/2026")


class DirectionTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.inspector = make_inspector()
        self.reference = make_reference_tree(self.admin)

    def page_paths(self):
        visit = make_visit(self.inspector, self.reference)
        return [
            reverse("core:home"),
            reverse("visits:list"),
            reverse("visits:detail", args=[visit.pk]),
            reverse("visits:scope", args=[visit.pk]),
            reverse("visits:execute", args=[visit.pk]),
            reverse("institutions:list"),
            reverse("catalog:list"),
            reverse("catalog:detail", args=[self.reference.pk]),
            reverse("governance:my_assignments"),
        ]

    def test_inspector_pages_are_arabic_and_right_to_left(self):
        self.client.force_login(self.inspector)
        for path in self.page_paths():
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                self.assertIn('dir="rtl"', html)
                self.assertIn('lang="ar"', html)

    def test_pages_declare_a_mobile_viewport(self):
        self.client.force_login(self.inspector)
        for path in self.page_paths():
            with self.subTest(path=path):
                html = self.client.get(path).content.decode()
                self.assertIn("width=device-width", html)

    def test_admin_pages_are_rtl(self):
        self.client.force_login(self.admin)
        for name in ["catalog:list", "governance:guide_list",
                     "governance:assignment_list", "catalog:submission_list",
                     "accounts:user_list"]:
            with self.subTest(name=name):
                html = self.client.get(reverse(name)).content.decode()
                self.assertIn('dir="rtl"', html)

    def test_navigation_hides_governance_links_from_inspectors(self):
        self.client.force_login(self.inspector)
        nav = self.client.get(reverse("core:home")).content.decode()
        links = set(re.findall(r'<a href="([^"]+)"', nav))
        self.assertNotIn(reverse("governance:guide_list"), links)
        self.assertNotIn(reverse("governance:assignment_list"), links)
        # The inspector still reaches their own assignments through a distinct URL.
        self.assertIn(reverse("governance:my_assignments"), links)

    def test_navigation_shows_governance_links_to_admins(self):
        self.client.force_login(self.admin)
        nav = self.client.get(reverse("core:home")).content.decode()
        links = set(re.findall(r'<a href="([^"]+)"', nav))
        self.assertIn(reverse("governance:guide_list"), links)
        self.assertIn(reverse("governance:assignment_list"), links)
        self.assertNotIn(reverse("governance:my_assignments"), links)


class ExecutionMarkupTests(TestCase):
    """The field screen is the one inspectors use on a phone."""

    def setUp(self):
        self.admin = make_admin()
        self.inspector = make_inspector()
        self.reference = make_reference_tree(self.admin)
        self.visit = make_visit(self.inspector, self.reference)
        self.client.force_login(self.inspector)

    def select(self, *codes):
        from visits.services import select_node

        for code in codes:
            select_node(visit=self.visit, node=self.visit.nodes.get(code=code))

    def execution_html(self):
        return self.client.get(
            reverse("visits:execute", args=[self.visit.pk])
        ).content.decode()

    def test_execution_screen_labels_each_result_choice(self):
        self.select("B1-D-1")
        html = self.execution_html()
        self.assertIn("مطابق", html)
        self.assertIn("غير مطابق", html)
        self.assertIn("غير منطبق", html)

    def test_not_applicable_and_not_matching_are_distinct_values(self):
        self.select("B1-D-1")
        html = self.execution_html()
        values = set(re.findall(r'name="result"[^>]*value="([^"]+)"', html))
        self.assertEqual(values, {"MATCH", "NOT_MATCH", "NOT_APPLICABLE"})

    def test_not_applicable_is_not_confused_with_not_matching(self):
        """The two outcomes must carry distinct stored codes."""
        from visits.models import ItemResult

        self.assertNotEqual(ItemResult.NOT_APPLICABLE, ItemResult.NOT_MATCH)
        self.select("B1-D-1")
        node = self.visit.nodes.get(code="B1-D-1")
        from visits.services import record_result

        record_result(visit=self.visit, node=node,
                      result=ItemResult.NOT_APPLICABLE)
        node.refresh_from_db()
        self.assertEqual(node.result, ItemResult.NOT_APPLICABLE)
        self.assertNotEqual(node.result, ItemResult.NOT_MATCH)

    def test_context_nodes_are_marked_as_not_counted(self):
        self.select("B1-D-1")
        html = self.execution_html()
        self.assertIn("سياق", html)
        self.assertIn("لا يُحتسب في الإنجاز", html)

    def test_scope_screen_separates_selected_from_context(self):
        self.select("B1-D-1")
        html = self.client.get(
            reverse("visits:scope", args=[self.visit.pk])
        ).content.decode()
        self.assertIn("مختار", html)
        self.assertIn("سياق", html)

    def test_progress_reports_done_over_total_and_percent(self):
        self.select("B1-D-1", "B1-D-2")
        html = self.execution_html()
        self.assertIn("0 / 2", html)
        self.assertIn("0%", html)

    def test_observation_field_is_available_for_each_item(self):
        self.select("B1-D-1")
        html = self.execution_html()
        self.assertIn('name="observation"', html)


class SeedDataIndependenceTests(TestCase):
    def test_a_fresh_system_is_usable_without_seed_data(self):
        """No business data is required for the application to function."""
        from institutions.models import Institution

        self.assertEqual(Institution.objects.count(), 0)

        admin = make_admin()
        self.client.force_login(admin)
        for name in ["core:home", "visits:list", "institutions:list",
                     "catalog:list", "governance:guide_list",
                     "governance:assignment_list", "accounts:user_list"]:
            with self.subTest(name=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)

    def test_no_template_hard_codes_business_records(self):
        """Demo names must come from the seed command, never from templates."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent / "templates"
        offenders = []
        for path in root.rglob("*.html"):
            text = path.read_text(encoding="utf-8")
            if "مركز التكوين المهني - النور" in text:
                offenders.append(str(path))
        self.assertEqual(offenders, [])