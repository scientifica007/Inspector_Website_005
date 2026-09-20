from django.db import models

from core.models import TimeStampedModel


class InstitutionKind(models.TextChoices):
    TRAINING_CENTER = "TRAINING_CENTER", "مركز تكوين مهني"
    APPRENTICESHIP = "APPRENTICESHIP", "مركز تكوين عن طريق التمهين"
    VOCATIONAL_SCHOOL = "VOCATIONAL_SCHOOL", "مدرسة مهنية"
    OTHER = "OTHER", "أخرى"


class Institution(TimeStampedModel):
    """An institution that can be the subject of an inspection visit.

    The model is intentionally small: a name, an identification code and a kind
    are enough to run scoping and reporting.
    """

    name = models.CharField(max_length=200, verbose_name="الاسم")
    code = models.CharField(
        max_length=50, blank=True, verbose_name="الرمز التعريفي"
    )
    kind = models.CharField(
        max_length=32,
        choices=InstitutionKind.choices,
        default=InstitutionKind.TRAINING_CENTER,
        verbose_name="النوع",
    )
    location = models.CharField(max_length=200, blank=True, verbose_name="الموقع")
    is_active = models.BooleanField(default=True, verbose_name="نشطة")

    class Meta:
        verbose_name = "مؤسسة"
        verbose_name_plural = "المؤسسات"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["name", "code"], name="unique_institution_name_code"
            )
        ]

    def __str__(self) -> str:
        return self.name