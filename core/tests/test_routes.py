"""Route coverage: every page an operator can reach must answer, not 404.

A missing or misnamed URL is invisible to service-level tests. These tests walk
the real named routes for both roles so that a broken link fails here instead of
in the operator's browser.
"""

from django.test import TestCase
from django.urls import NoReverseMatch, reverse

from core.testing import (
    add_item,
    make_admin,
    make_guide,
    make_inspector,
    make_institution,
    make_private_reference,
    make_reference_tree,
)
from visits.services import create_visit

# Pages that take no URL arguments.
STATIC_PAGES = {
    "admin": [
        "core:home",
        "visits:list",
        "institutions:list",
        "institutions:create",
        "catalog:list",
        "catalog:create_shared",
        "catalog:create_private",
        "catalog:submission_list",
        "governance:guide_list",
        "governance:guide_create",
        "governance:assignment_list",
        "accounts:user_list",
        "accounts:user_create",
        "accounts:profile",
    ],
    "inspector": [
        "core:home",
        "visits:list",
        "visits:create",
        "catalog:list",
        "catalog:create_private",
        "governance:my_assignments",
        "accounts:profile",
    ],
}


class StaticRouteTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.inspector = make_inspector()

    def test_admin_pages_all_answer(self):
        self.client.force_login(self.admin)
        for name in STATIC_PAGES["admin"]:
            with self.subTest(name=name):
                response = self.client.get(reverse(name))
                self.assertIn(
                    response.status_code, (200,),
                    f"{name} returned {response.status_code}",
                )

    def test_inspector_pages_all_answer(self):
        self.client.force_login(self.inspector)
        for name in STATIC_PAGES["inspector"]:
            with self.subTest(name=name):
                response = self.client.get(reverse(name))
                self.assertIn(
                    response.status_code, (200,),
                    f"{name} returned {response.status_code}",
                )

    def test_every_documented_route_name_reverses(self):
        """Catches typos in ``{% url %}`` names and in this list."""
        for role, names in STATIC_PAGES.items():
            for name in names:
                with self.subTest(role=role, name=name):
                    try:
                        reverse(name)
                    except NoReverseMatch:  # pragma: no cover - failure path
                        self.fail(f"{name} does not reverse")

    def test_admin_is_denied_visit_creation_and_is_not_offered_it(self):
        """Admins supervise rather than execute, and the UI must agree."""
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("visits:create")).status_code, 403)
        for name in ["core:home", "visits:list"]:
            html = self.client.get(reverse(name)).content.decode()
            self.assertNotIn(reverse("visits:create"), html)


class DetailRouteTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.inspector = make_inspector()
        self.reference = make_reference_tree(self.admin)
        self.institution = make_institution()
        self.visit = create_visit(
            actor=self.inspector,
            institution=self.institution,
            visit_date="2026-06-01",
            reference=self.reference,
        )
        self.private = make_private_reference(self.inspector)

    def test_inspector_detail_pages_answer(self):
        self.client.force_login(self.inspector)
        paths = [
            reverse("visits:detail", args=[self.visit.pk]),
            reverse("visits:scope", args=[self.visit.pk]),
            reverse("visits:execute", args=[self.visit.pk]),
            reverse("visits:export", args=[self.visit.pk]),
            reverse("visits:edit", args=[self.visit.pk]),
            reverse("catalog:detail", args=[self.reference.pk]),
            reverse("catalog:detail", args=[self.private.pk]),
            reverse("catalog:edit", args=[self.private.pk]),
            reverse("institutions:detail", args=[self.institution.pk]),
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_admin_detail_pages_answer(self):
        self.client.force_login(self.admin)
        guide = make_guide(self.admin, self.reference, codes=["B1-1"])
        paths = [
            reverse("catalog:detail", args=[self.reference.pk]),
            reverse("catalog:edit", args=[self.reference.pk]),
            reverse("catalog:node_create", args=[self.reference.pk]),
            reverse("governance:guide_detail", args=[guide.pk]),
            reverse("governance:guide_edit", args=[guide.pk]),
            reverse("governance:assignment_create", args=[self.visit.pk]),
            reverse("visits:detail", args=[self.visit.pk]),
            reverse("visits:export", args=[self.visit.pk]),
            reverse("accounts:user_edit", args=[self.inspector.pk]),
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_guide_target_add_accepts_post_and_rejects_get(self):
        """The endpoint mutates state, so it must not answer a plain GET."""
        guide = make_guide(self.admin, self.reference, codes=[])
        self.client.force_login(self.admin)
        url = reverse("governance:guide_target_add", args=[guide.pk])
        self.assertEqual(self.client.get(url).status_code, 405)

        response = self.client.post(url, {"code": "B1-1"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            list(guide.targets.values_list("code", flat=True)), ["B1-1"]
        )

    def test_guide_target_add_rejects_a_code_absent_from_the_reference(self):
        guide = make_guide(self.admin, self.reference, codes=[])
        self.client.force_login(self.admin)
        url = reverse("governance:guide_target_add", args=[guide.pk])
        self.client.post(url, {"code": "لا-يوجد"})
        self.assertEqual(guide.targets.count(), 0)

    def test_admin_reference_submission_pages_answer(self):
        from catalog.services import create_submission

        submission = create_submission(
            reference=self.private, actor=self.inspector
        )
        self.client.force_login(self.admin)
        for path in [
            reverse("catalog:submission_list"),
            reverse("catalog:submission_detail", args=[submission.pk]),
        ]:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_admin_assignment_detail_answers(self):
        from core.testing import make_assignment

        assignment = make_assignment(
            self.admin, self.visit, entries=[("B1-D-1", True, True)]
        )
        self.client.force_login(self.admin)
        path = reverse("governance:assignment_detail", args=[assignment.pk])
        self.assertEqual(self.client.get(path).status_code, 200)

    def test_unknown_reference_returns_404_not_a_crash(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("catalog:detail", args=[999999]))
        self.assertEqual(response.status_code, 404)

    def test_unknown_visit_returns_404_not_a_crash(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("visits:detail", args=[999999]))
        self.assertEqual(response.status_code, 404)