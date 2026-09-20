from django.contrib.auth.models import AbstractUser
from django.db import models


class Role(models.TextChoices):
    ADMIN = "ADMIN", "مدير النظام"
    INSPECTOR = "INSPECTOR", "مفتش"


class User(AbstractUser):
    """Application user.

    The role drives authorization on the server. Both roles share one user
    table so that a person can be promoted without a data migration.
    """

    role = models.CharField(
        max_length=16,
        choices=Role.choices,
        default=Role.INSPECTOR,
        verbose_name="الدور",
    )
    display_name = models.CharField(
        max_length=150, blank=True, verbose_name="الاسم الظاهر"
    )

    class Meta:
        verbose_name = "مستخدم"
        verbose_name_plural = "المستخدمون"

    def __str__(self) -> str:
        return self.display_name or self.get_full_name() or self.username

    @property
    def is_admin_role(self) -> bool:
        return self.role == Role.ADMIN

    @property
    def is_inspector_role(self) -> bool:
        return self.role == Role.INSPECTOR