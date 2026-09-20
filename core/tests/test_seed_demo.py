"""Seed command tests: useful demo data, never required, never destructive."""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from accounts.models import Role, User
from catalog.models import Reference
from governance.models import Guide
from institutions.models import Institution
from visits.models import Visit


class SeedDemoTests(TestCase):
    def run_seed(self, *args):
        out = StringIO()
        call_command("seed_demo", *args, stdout=out)
        return out.getvalue()

    def test_seed_creates_the_documented_demo_accounts(self):
        self.run_seed()
        self.assertEqual(User.objects.get(username="admin").role, Role.ADMIN)
        self.assertEqual(
            User.objects.get(username="inspector").role, Role.INSPECTOR
        )
        self.assertTrue(
            User.objects.get(username="inspector").check_password(
                "inspector-demo-123"
            )
        )

    def test_seed_creates_institutions_references_and_guides(self):
        self.run_seed()
        self.assertGreaterEqual(Institution.objects.count(), 3)
        reference = Reference.objects.get(title="مرجع التفتيش البيداغوجي")
        codes = set(reference.nodes.values_list("code", flat=True))
        self.assertIn("PED-DOC-1", codes)
        self.assertIn("PED-DOC-2", codes)
        self.assertGreaterEqual(Guide.objects.count(), 2)

    def test_seed_creates_a_draft_a_local_only_visit_and_a_completed_visit(self):
        self.run_seed()
        statuses = set(
            Visit.objects.filter(inspector__username="inspector").values_list(
                "status", flat=True
            )
        )
        self.assertEqual(statuses, {"DRAFT", "COMPLETED"})
        local_only = Visit.objects.filter(
            inspector__username="inspector", source_reference__isnull=True
        )
        self.assertTrue(local_only.exists())

    def test_seed_is_idempotent(self):
        self.run_seed()
        counts = (
            User.objects.count(),
            Institution.objects.count(),
            Reference.objects.count(),
            Visit.objects.count(),
        )
        self.run_seed()
        self.assertEqual(
            counts,
            (
                User.objects.count(),
                Institution.objects.count(),
                Reference.objects.count(),
                Visit.objects.count(),
            ),
        )

    def test_seed_never_resets_a_password_of_an_existing_user(self):
        self.run_seed()
        admin = User.objects.get(username="admin")
        admin.set_password("changed-by-hand")
        admin.save()
        self.run_seed()
        admin.refresh_from_db()
        self.assertTrue(admin.check_password("changed-by-hand"))

    def test_reset_only_removes_demo_records(self):
        real_institution = Institution.objects.create(name="مؤسسة حقيقية", code="REAL")
        real_user = User.objects.create_user(
            username="real", password="x-123456", role=Role.INSPECTOR
        )
        self.run_seed()
        self.run_seed("--reset")
        self.assertTrue(Institution.objects.filter(pk=real_institution.pk).exists())
        self.assertTrue(User.objects.filter(pk=real_user.pk).exists())
        self.assertTrue(Visit.objects.filter(inspector__username="inspector").exists())

    def test_seed_is_not_required_for_the_application_to_work(self):
        """A fresh database must serve pages without any demo data."""
        self.assertEqual(Visit.objects.count(), 0)
        admin = User.objects.create_user(
            username="fresh", password="x-123456", role=Role.ADMIN
        )
        self.client.force_login(admin)
        self.assertEqual(self.client.get("/").status_code, 200)