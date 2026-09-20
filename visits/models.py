from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from core.models import TimeStampedModel
from catalog.models import NodeType, ValueType


class VisitStatus(models.TextChoices):
    DRAFT = "DRAFT", "مسودة"
    COMPLETED = "COMPLETED", "مكتملة"


class NodeStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "نشط"
    EXCLUDED = "EXCLUDED", "مستبعد"


class Origin(models.TextChoices):
    """Why an element is part of the visit scope.

    The value is written once, when the element enters the scope, and is never
    overwritten. A Guide or an Assignment that later touches the element only
    adds obligations; it does not erase the original reason.
    """

    LEGACY = "LEGACY", "موروث من المرجع"
    MANUAL = "MANUAL", "اختيار يدوي"
    GUIDE = "GUIDE", "من دليل"
    ASSIGNMENT = "ASSIGNMENT", "من تكليف"
    LOCAL = "LOCAL", "محتوى محلي"


class ItemResult(models.TextChoices):
    NONE = "", "—"
    MATCH = "MATCH", "مطابق"
    NOT_MATCH = "NOT_MATCH", "غير مطابق"
    NOT_APPLICABLE = "NOT_APPLICABLE", "غير منطبق"


class Visit(TimeStampedModel):
    """A single inspection event.

    A visit is not "free", "guided" or "locked". Suggestions and constraints
    apply to individual elements inside it.
    """

    title = models.CharField(max_length=250, verbose_name="عنوان الزيارة")
    institution = models.ForeignKey(
        "institutions.Institution",
        on_delete=models.PROTECT,
        related_name="visits",
        verbose_name="المؤسسة",
    )
    inspector = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="visits",
        verbose_name="المفتش",
    )
    visit_date = models.DateField(verbose_name="تاريخ الزيارة")
    status = models.CharField(
        max_length=16,
        choices=VisitStatus.choices,
        default=VisitStatus.DRAFT,
        verbose_name="الحالة",
    )
    notes = models.TextField(blank=True, verbose_name="ملاحظات عامة")

    # --- Frozen source provenance -------------------------------------------
    source_reference = models.ForeignKey(
        "catalog.Reference",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="visits",
        verbose_name="المرجع المصدر",
    )
    source_reference_title = models.CharField(
        max_length=250, blank=True, verbose_name="عنوان المرجع وقت الإنشاء"
    )
    source_reference_visibility = models.CharField(
        max_length=16, blank=True, verbose_name="نطاق المرجع وقت الإنشاء"
    )
    source_reference_revision = models.PositiveIntegerField(
        null=True, blank=True, verbose_name="مراجعة المرجع وقت الإنشاء"
    )
    #: Institution identity is snapshotted too, so that renaming or archiving an
    #: institution never rewrites a past visit.
    institution_name_snapshot = models.CharField(
        max_length=200, blank=True, verbose_name="اسم المؤسسة وقت الإنشاء"
    )
    completed_at = models.DateTimeField(
        null=True, blank=True, verbose_name="تاريخ الإكمال"
    )
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="completed_visits",
        verbose_name="أكملها",
    )

    class Meta:
        verbose_name = "زيارة"
        verbose_name_plural = "الزيارات"
        ordering = ["-visit_date", "-id"]
        indexes = [
            models.Index(fields=["inspector", "status"]),
            models.Index(fields=["institution", "visit_date"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(status=VisitStatus.DRAFT)
                    | models.Q(completed_at__isnull=False)
                ),
                name="completed_visit_has_completed_at",
            )
        ]

    def __str__(self) -> str:
        return f"{self.title} — {self.institution_name_snapshot or self.institution}"

    @property
    def is_draft(self) -> bool:
        return self.status == VisitStatus.DRAFT

    @property
    def is_completed(self) -> bool:
        return self.status == VisitStatus.COMPLETED

    @property
    def source_reference_label(self) -> str:
        """Historical label that survives deletion of the source reference."""
        if not self.source_reference_title:
            return "بدون مرجع"
        reference = self.source_reference
        if reference is None or reference.is_deleted:
            return f"{self.source_reference_title} (محذوف)"
        return self.source_reference_title

    def active_nodes(self):
        return self.nodes.filter(status=NodeStatus.ACTIVE)

    def selected_nodes(self):
        return self.nodes.filter(status=NodeStatus.ACTIVE, is_selected=True)


class VisitNode(TimeStampedModel):
    """A frozen copy of a reference element, or content authored in the visit.

    Nothing on this row reads from the live reference. Once written, the node is
    the visit's own truth.
    """

    visit = models.ForeignKey(
        Visit, on_delete=models.CASCADE, related_name="nodes", verbose_name="الزيارة"
    )
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="children",
        verbose_name="العقدة الأم",
    )
    node_type = models.CharField(
        max_length=20, choices=NodeType.choices, verbose_name="النوع"
    )
    #: Stable identifier copied from the reference, or generated for local content.
    code = models.CharField(max_length=120, verbose_name="المعرّف الثابت")
    #: Informational link to the originating catalog node. Deliberately not a
    #: foreign key: the source may be deleted without touching the visit.
    source_node_id = models.IntegerField(null=True, blank=True, verbose_name="معرّف العنصر المصدر")
    title = models.CharField(max_length=300, verbose_name="العنوان")
    body = models.TextField(blank=True, verbose_name="التفصيل")
    value_type = models.CharField(
        max_length=16,
        choices=ValueType.choices,
        default=ValueType.NONE,
        verbose_name="نوع القيمة",
    )
    position = models.IntegerField(default=0, verbose_name="الترتيب")

    origin = models.CharField(
        max_length=16,
        choices=Origin.choices,
        null=True,
        blank=True,
        verbose_name="المصدر",
        help_text=(
            "يُكتب مرة واحدة عند دخول العنصر إلى النطاق ولا يُستبدل لاحقًا. "
            "القيمة الفارغة تعني عنصر هيكلي لم يدخل النطاق."
        ),
    )
    is_selected = models.BooleanField(
        default=False,
        verbose_name="داخل النطاق",
        help_text="False تعني عقدة سياق فقط: تُعرض لكنها لا تدخل حساب الإنجاز.",
    )
    status = models.CharField(
        max_length=16,
        choices=NodeStatus.choices,
        default=NodeStatus.ACTIVE,
        verbose_name="الحالة",
    )

    # --- Baseline constraints (intrinsic to the snapshot) -------------------
    baseline_scope_locked = models.BooleanField(
        default=False, verbose_name="قيد نطاق أساسي"
    )
    baseline_completion_required = models.BooleanField(
        default=False, verbose_name="متطلب إتمام أساسي"
    )

    # --- Execution data ------------------------------------------------------
    result = models.CharField(
        max_length=16,
        choices=ItemResult.choices,
        blank=True,
        default=ItemResult.NONE,
        verbose_name="النتيجة",
    )
    observation = models.TextField(blank=True, verbose_name="ملاحظة / مشاهدة")
    value_text = models.TextField(blank=True, verbose_name="قيمة نصية")
    value_number = models.DecimalField(
        max_digits=14, decimal_places=3, null=True, blank=True, verbose_name="قيمة عددية"
    )
    value_date = models.DateField(null=True, blank=True, verbose_name="قيمة تاريخ")
    value_boolean = models.BooleanField(null=True, blank=True, verbose_name="قيمة منطقية")

    excluded_at = models.DateTimeField(null=True, blank=True, verbose_name="تاريخ الاستبعاد")
    #: Set on descendants that were excluded because an ancestor was excluded.
    #: Restoring the ancestor restores exactly this group, so no data is lost.
    excluded_via = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="excluded_descendants",
        verbose_name="استُبعد ضمن",
    )

    class Meta:
        verbose_name = "عنصر زيارة"
        verbose_name_plural = "عناصر الزيارة"
        ordering = ["position", "id"]
        indexes = [
            models.Index(fields=["visit", "status", "is_selected"]),
            models.Index(fields=["visit", "parent", "position"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["visit", "code"], name="unique_visit_node_code"
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(status=NodeStatus.EXCLUDED, excluded_at__isnull=False)
                    | models.Q(status=NodeStatus.ACTIVE)
                ),
                name="excluded_node_has_timestamp",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.title}"

    # -- Type helpers ---------------------------------------------------------
    @property
    def is_branch(self) -> bool:
        return self.node_type == NodeType.BRANCH

    @property
    def is_description(self) -> bool:
        return self.node_type == NodeType.DESCRIPTION

    @property
    def is_checklist_item(self) -> bool:
        return self.node_type == NodeType.CHECKLIST_ITEM

    @property
    def is_active(self) -> bool:
        return self.status == NodeStatus.ACTIVE

    @property
    def is_excluded(self) -> bool:
        return self.status == NodeStatus.EXCLUDED

    @property
    def is_context_only(self) -> bool:
        return not self.is_selected

    @property
    def carries_value(self) -> bool:
        return self.is_description and self.value_type != ValueType.NONE

    # -- Execution helpers ----------------------------------------------------
    @property
    def is_completable(self) -> bool:
        """Whether this element can record a result or a value.

        Structural branches are never completable, which is why
        ``completion_required`` on a branch is applied to its completable
        descendants instead of to the empty structural node itself.
        """
        return self.is_checklist_item or self.carries_value

    @property
    def has_recorded_value(self) -> bool:
        if self.value_type == ValueType.NONE:
            return bool(self.value_text.strip())
        if self.value_type == ValueType.TEXT:
            return bool(self.value_text.strip())
        if self.value_type == ValueType.NUMBER:
            return self.value_number is not None
        if self.value_type == ValueType.DATE:
            return self.value_date is not None
        if self.value_type == ValueType.BOOLEAN:
            return self.value_boolean is not None
        return False

    @property
    def is_fulfilled(self) -> bool:
        """True when the element has produced the evidence completion needs."""
        if self.is_checklist_item:
            return bool(self.result) and self.result != ItemResult.NONE
        if self.carries_value:
            return self.has_recorded_value
        return False

    @property
    def is_counted_for_progress(self) -> bool:
        return self.is_active and self.is_selected and self.is_completable

    def clean(self):
        if self.parent_id and self.visit_id and self.parent.visit_id != self.visit_id:
            raise ValidationError({"parent": "العقدة الأم تنتمي لزيارة أخرى."})
        if self.result and not self.is_checklist_item:
            raise ValidationError({"result": "النتيجة تُسجَّل لبنود التفتيش فقط."})