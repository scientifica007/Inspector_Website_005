"""Authentication and role/permission tests."""

from django.test import TestCase
from django.urls import reverse

from accounts.models import Role, User
from core.testing import make_admin, make_inspector


class AuthenticationTests(TestCase):
    def test_login_page_is_reachable(self):
        response = self.client.get(reverse("accounts:login"))
        self.assertEqual(response.status_code, 200)

    def test_login_with_valid_credentials(self):
        make_inspector("insp", "strong-pass-123")
        response = self.client.post(
            reverse("accounts:login"), {"username": "insp", "password": "strong-pass-123"}
        )
        self.assertEqual(response.status_code, 302)

    def test_login_with_wrong_password_fails(self):
        make_inspector("insp", "strong-pass-123")
        response = self.client.post(
            reverse("accounts:login"), {"username": "insp", "password": "nope"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_logout(self):
        user = make_inspector("insp")
        self.client.force_login(user)
        response = self.client.post(reverse("accounts:logout"))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_home_requires_login(self):
        response = self.client.get(reverse("core:home"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response.url)

    def test_home_is_reachable_after_login(self):
        self.client.force_login(make_inspector("insp"))
        self.assertEqual(self.client.get(reverse("core:home")).status_code, 200)

    def test_registration_creates_an_inspector_only(self):
        response = self.client.post(
            reverse("accounts:register"),
            {
                "username": "newbie",
                "password1": "strong-pass-123",
                "password2": "strong-pass-123",
                "role": Role.ADMIN,  # must be ignored: self-service is inspector-only
            },
        )
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(username="newbie")
        self.assertEqual(user.role, Role.INSPECTOR)

    def test_registration_rejects_mismatched_passwords(self):
        response = self.client.post(
            reverse("accounts:register"),
            {
                "username": "newbie",
                "password1": "strong-pass-123",
                "password2": "other-pass-123",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="newbie").exists())


class RoleHelperTests(TestCase):
    def test_admin_role_predicates(self):
        admin = make_admin()
        self.assertTrue(admin.is_admin_role)
        self.assertFalse(admin.is_inspector_role)

    def test_inspector_role_predicates(self):
        inspector = make_inspector()
        self.assertTrue(inspector.is_inspector_role)
        self.assertFalse(inspector.is_admin_role)

    def test_default_role_is_inspector(self):
        user = User.objects.create_user(username="plain", password="strong-pass-123")
        self.assertEqual(user.role, Role.INSPECTOR)


class AdminAreaTests(TestCase):
    def test_inspector_cannot_open_the_user_admin(self):
        self.client.force_login(make_inspector("insp"))
        response = self.client.get(reverse("accounts:user_list"))
        self.assertEqual(response.status_code, 403)

    def test_admin_can_open_the_user_admin(self):
        self.client.force_login(make_admin("boss"))
        response = self.client.get(reverse("accounts:user_list"))
        self.assertEqual(response.status_code, 200)

    def test_admin_can_create_another_admin(self):
        self.client.force_login(make_admin("boss"))
        response = self.client.post(
            reverse("accounts:user_create"),
            {
                "username": "boss2",
                "password1": "strong-pass-123",
                "password2": "strong-pass-123",
                "role": Role.ADMIN,
                "display_name": "مدير ثانٍ",
            },
        )
        self.assertEqual(response.status_code, 302)
        created = User.objects.get(username="boss2")
        self.assertEqual(created.role, Role.ADMIN)
        self.assertTrue(created.check_password("strong-pass-123"))

    def test_admin_cannot_lock_their_own_account(self):
        admin = make_admin("boss")
        self.client.force_login(admin)
        response = self.client.post(
            reverse("accounts:user_toggle_active", args=[admin.pk])
        )
        self.assertEqual(response.status_code, 302)
        admin.refresh_from_db()
        self.assertTrue(admin.is_active)

    def test_admin_can_deactivate_another_user(self):
        admin = make_admin("boss")
        other = make_inspector("other")
        self.client.force_login(admin)
        self.client.post(reverse("accounts:user_toggle_active", args=[other.pk]))
        other.refresh_from_db()
        self.assertFalse(other.is_active)

    def test_deactivated_user_cannot_log_in(self):
        user = make_inspector("ghost", "strong-pass-123")
        user.is_active = False
        user.save()
        response = self.client.post(
            reverse("accounts:login"), {"username": "ghost", "password": "strong-pass-123"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)