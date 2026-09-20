"""Institution management tests."""

from django.test import TestCase
from django.urls import reverse

from core.testing import make_admin, make_institution, make_inspector
from institutions.models import Institution, InstitutionKind


class InstitutionCrudTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.inspector = make_inspector()

    def test_list_requires_login(self):
        self.assertEqual(self.client.get(reverse("institutions:list")).status_code, 302)

    def test_inspector_sees_the_list(self):
        make_institution("معهد أ", code="A")
        self.client.force_login(self.inspector)
        response = self.client.get(reverse("institutions:list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "معهد أ")

    def test_inspector_cannot_create(self):
        self.client.force_login(self.inspector)
        response = self.client.get(reverse("institutions:create"))
        self.assertEqual(response.status_code, 403)

    def test_admin_can_create(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("institutions:create"),
            {
                "name": "مركز تكوين جديد",
                "code": "NEW-1",
                "kind": InstitutionKind.APPRENTICESHIP,
                "location": "الجزائر",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        created = Institution.objects.get(code="NEW-1")
        self.assertEqual(created.kind, InstitutionKind.APPRENTICESHIP)

    def test_admin_can_edit(self):
        institution = make_institution("قديم", code="OLD")
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("institutions:edit", args=[institution.pk]),
            {
                "name": "جديد",
                "code": "OLD",
                "kind": InstitutionKind.TRAINING_CENTER,
                "location": "",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        institution.refresh_from_db()
        self.assertEqual(institution.name, "جديد")

    def test_inspector_cannot_edit(self):
        institution = make_institution("معهد", code="X")
        self.client.force_login(self.inspector)
        response = self.client.post(
            reverse("institutions:edit", args=[institution.pk]),
            {"name": "تغيير", "code": "X", "kind": InstitutionKind.TRAINING_CENTER},
        )
        self.assertEqual(response.status_code, 403)
        institution.refresh_from_db()
        self.assertEqual(institution.name, "معهد")

    def test_inactive_institution_is_hidden_from_inspectors(self):
        make_institution("نشطة", code="ON")
        make_institution("موقوفة", code="OFF", is_active=False)
        self.client.force_login(self.inspector)
        response = self.client.get(reverse("institutions:list"))
        self.assertContains(response, "نشطة")
        self.assertNotContains(response, "موقوفة")

    def test_admin_sees_inactive_institutions(self):
        make_institution("موقوفة", code="OFF", is_active=False)
        self.client.force_login(self.admin)
        response = self.client.get(reverse("institutions:list"))
        self.assertContains(response, "موقوفة")

    def test_search_filters_by_name_and_code(self):
        make_institution("معهد النور", code="NUR")
        make_institution("معهد الفجر", code="FAJ")
        self.client.force_login(self.inspector)
        response = self.client.get(reverse("institutions:list"), {"q": "NUR"})
        self.assertContains(response, "معهد النور")
        self.assertNotContains(response, "معهد الفجر")

    def test_inspector_cannot_open_inactive_institution_detail(self):
        institution = make_institution("موقوفة", code="OFF", is_active=False)
        self.client.force_login(self.inspector)
        response = self.client.get(reverse("institutions:detail", args=[institution.pk]))
        self.assertEqual(response.status_code, 302)