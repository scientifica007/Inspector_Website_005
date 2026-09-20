from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from core.models import TimeStampedModel
from catalog.models import NodeType


class GuideStatus(models.TextChoices):
    DRAFT = "DRAFT", "مسودة"
    PUBLISHED = "PUBLISHED", "منشور"
    ARCHIVED = "ARCHIVED", "مؤرشف"


class Guide(TimeStampedModel):
    """An optional, reusable suggestion attached to one shared reference.

    A guide never locks or mandates anything. Applying it only adds elements to
    the visit scope that are not already there, and never rewrites a visit that
    was created earlier.
    """

    title = models.CharField(max_length=250, verbose_name="عنوان الدليل")
    description = models.TextField(blank=True, verbose_name="وصف الدليل")
    reference = models.ForeignKey(
        "catalog.Reference",
        on_delete=models.PROTECT,
        related_name="guides",
        verbose_name="المرجع المشترك",
    )
    status = models.CharField(
        max_length=16, choices=GuideStatus.choices, default=GuideStatus.DRAFT,
        verbose_name="الحالة",
    )
    version = models.PositiveIntegerField(default=1, verbose_name="الإصدار")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="created_guides",
        verbose_name="أنشأه",
    )

    class Meta:
        verbose_name = "دليل"
        verbose_name_plural = "الأدلة"
        ordering = ["title", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["reference", "title"], name="unique_guide_title_per_reference"
            )
        ]

    def __str__(self) -> str:
        return self.title

    @property
    def is_applicable(self) -> bool:
        return self.status == GuideStatus.PUBLISHED

    def clean(self):
        if self.reference_id and not self.reference.is_shared:
            raise ValidationError(
                {"reference": "الدليل يجب أن يرتبط بمرجع مشترك."}
            )


class GuideTarget(TimeStampedModel):
    """One suggested element inside the guide's reference.

    Matching against a visit uses ``code`` only: that is the stable identifier
    shared by the reference, the frozen snapshot and the export. Title and type
    are stored for the administrator's convenience and may drift from the live
    reference without affecting frozen visits.
    """

    guide = models.ForeignKey(
        Guide, on_delete=models.CASCADE, related_name="targets", verbose_name="الدليل"
    )
    code = models.CharField(max_length=120, verbose_name="معرّف العنصر")
    title_snapshot = models.CharField(max_length=300, verbose_name="عنوان وقت الإضافة")
    node_type_snapshot = models.CharField(
        max_length=20, choices=NodeType.choices, verbose_name="نوع وقت الإضافة"
    )
    position = models.IntegerField(default=0, verbose_name="الترتيب")

    class Meta:
        verbose_name = "عنصر دليل"
        verbose_name_plural = "عناصر الدليل"
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["guide", "code"], name="unique_guide_target_code"
            )
        ]

    def __str__(self) -> str:
        return f"{self.guide.title} → {self.code}"


class AssignmentStatus(models.TextChoices):
    DRAFT = "DRAFT", "مسودة إدارية"
    ISSUED = "ISSUED", "صادر"
    REVOKED = "REVOKED", "ملغى"


class Assignment(TimeStampedModel):
    """An official assignment attached to one existing draft visit.

    Draft assignments are invisible to the inspector. Issuing is atomic: either
    every entry is valid and applied, or nothing changes at all.
    """

    visit = models.ForeignKey(
        "visits.Visit",
        on_delete=models.PROTECT,
        related_name="assignments",
        verbose_name="الزيارة",
    )
    title = models.CharField(max_length=250, verbose_name="عنوان التكليف")
    instruction = models.TextField(blank=True, verbose_name="نص التكليف")
    status = models.CharField(
        max_length=16,
        choices=AssignmentStatus.choices,
        default=AssignmentStatus.DRAFT,
        verbose_name="الحالة",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="created_assignments",
        verbose_name="أنشأه",
    )
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="issued_assignments",
        verbose_name="أصدره",
    )
    issued_at = models.DateTimeField(null=True, blank=True, verbose_name="تاريخ الإصدار")

    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="revoked_assignments",
        verbose_name="ألغاه",
    )
    revoked_at = models.DateTimeField(null=True, blank=True, verbose_name="تاريخ الإلغاء")
    revoke_reason = models.TextField(blank=True, verbose_name="سبب الإلغاء")

    class Meta:
        verbose_name = "تكليف"
        verbose_name_plural = "التكليفات"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(status=AssignmentStatus.DRAFT)
                    | models.Q(issued_at__isnull=False)
                ),
                name="non_draft_assignment_has_issued_at",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status=AssignmentStatus.REVOKED)
                    | models.Q(revoked_at__isnull=False, revoked_by__isnull=False)
                ),
                name="revoked_assignment_has_audit",
            ),
        ]
        indexes = [
            models.Index(fields=["visit", "status"]),
        ]

    def __str__(self) -> str:
        return self.title

    @property
    def is_issued(self) -> bool:
        return self.status == AssignmentStatus.ISSUED

    @property
    def is_draft(self) -> bool:
        return self.status == AssignmentStatus.DRAFT

    @property
    def is_revoked(self) -> bool:
        return self.status == AssignmentStatus.REVOKED

    @property
    def is_effective(self) -> bool:
        """Only issued assignments still impose obligations."""
        return self.status == AssignmentStatus.ISSUED


class AssignmentEntry(TimeStampedModel):
    """A single obligation targeting one element of a visit's frozen snapshot.

    ``scope_locked`` and ``completion_required`` are independent. When an entry
    targets a branch, its obligations descend to the branch's subtree so that
    the constraint cannot be bypassed by removing children.
    """

    assignment = models.ForeignKey(
        Assignment, on_delete=models.CASCADE, related_name="entries",
        verbose_name="التكليف",
    )
    target_node = models.ForeignKey(
        "visits.VisitNode",
        on_delete=models.PROTECT,
        related_name="assignment_entries",
        verbose_name="العنصر المستهدف",
    )
    scope_locked = models.BooleanField(default=False, verbose_name="قيد النطاق")
    completion_required = models.BooleanField(
        default=False, verbose_name="متطلب إتمام"
    )
    note = models.TextField(blank=True, verbose_name="ملاحظة التكليف")

    class Meta:
        verbose_name = "بند تكليف"
        verbose_name_plural = "بنود التكليف"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["assignment", "target_node"],
                name="unique_assignment_entry_target",
            ),
            models.CheckConstraint(
                condition=models.Q(scope_locked=True) | models.Q(completion_required=True),
                name="assignment_entry_imposes_something",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.assignment.title} → {self.target_node.code}"

    def clean(self):
        if self.assignment_id and self.target_node_id:
            if self.assignment.visit_id != self.target_node.visit_id:
                raise ValidationError(
                    {"target_node": "العنصر المستهدف لا ينتمي إلى زيارة التكليف."}
                )


class GuideApplication(TimeStampedModel):
    """Record that a guide was applied to a visit.

    This makes application idempotent: applying the same guide to the same visit
    twice adds nothing the second time and does not duplicate audit rows. The
    stored guide title and version keep the historical fact readable even after
    the guide is edited or archived.
    """

    visit = models.ForeignKey(
        "visits.Visit",
        on_delete=models.CASCADE,
        related_name="guide_applications",
        verbose_name="الزيارة",
    )
    guide = models.ForeignKey(
        Guide,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="applications",
        verbose_name="الدليل",
    )
    guide_title_snapshot = models.CharField(max_length=250, verbose_name="عنوان الدليل وقت التطبيق")
    guide_version_snapshot = models.PositiveIntegerField(default=1, verbose_name="إصدار الدليل وقت التطبيق")
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="guide_applications",
        verbose_name="طبّقه",
    )
    added_count = models.PositiveIntegerField(default=0, verbose_name="عدد العناصر المضافة")
    skipped_count = models.PositiveIntegerField(default=0, verbose_name="عدد العناصر المتخطاة")
    skipped_codes = models.JSONField(default=list, blank=True, verbose_name="معرّفات متخطاة")

    class Meta:
        verbose_name = "تطبيق دليل"
        verbose_name_plural = "تطبيقات الأدلة"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["visit", "guide"], name="unique_guide_application_per_visit"
            )
        ]

    def __str__(self) -> str:
        return f"{self.guide_title_snapshot} → زيارة #{self.visit_id}"