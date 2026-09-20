from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from core.models import TimeStampedModel


class ReferenceVisibility(models.TextChoices):
    SHARED = "SHARED", "مرجع مشترك"
    PRIVATE = "PRIVATE", "مرجع خاص"


class NodeType(models.TextChoices):
    BRANCH = "BRANCH", "فرع / قسم"
    DESCRIPTION = "DESCRIPTION", "وصف / مواصفة"
    CHECKLIST_ITEM = "CHECKLIST_ITEM", "بند تفتيش"


class ValueType(models.TextChoices):
    NONE = "NONE", "بدون قيمة"
    TEXT = "TEXT", "نص"
    NUMBER = "NUMBER", "عدد"
    DATE = "DATE", "تاريخ"
    BOOLEAN = "BOOLEAN", "نعم / لا"


#: Allowed parent node types for each node type. ``None`` means "root allowed".
ALLOWED_PARENTS = {
    NodeType.BRANCH: {None, NodeType.BRANCH},
    NodeType.DESCRIPTION: {None, NodeType.BRANCH},
    NodeType.CHECKLIST_ITEM: {NodeType.BRANCH, NodeType.DESCRIPTION},
}

#: Node types that may appear as the target of a Guide or an Assignment entry.
TARGETABLE_NODE_TYPES = {
    NodeType.BRANCH,
    NodeType.DESCRIPTION,
    NodeType.CHECKLIST_ITEM,
}


class ReferenceQuerySet(models.QuerySet):
    def shared(self):
        return self.filter(visibility=ReferenceVisibility.SHARED)

    def private_of(self, user):
        return self.filter(visibility=ReferenceVisibility.PRIVATE, owner=user)

    def visible_to(self, user):
        """Shared references plus the caller's own private references."""
        return self.filter(
            models.Q(visibility=ReferenceVisibility.SHARED)
            | models.Q(visibility=ReferenceVisibility.PRIVATE, owner=user)
        )

    def live(self):
        return self.filter(is_active=True, deleted_at__isnull=True)


class Reference(TimeStampedModel):
    """A citable inspection reference.

    Several references are valid in parallel; there is deliberately no single
    "master" reference that supersedes the others.
    """

    title = models.CharField(max_length=250, verbose_name="العنوان")
    description = models.TextField(blank=True, verbose_name="وصف مختصر")
    visibility = models.CharField(
        max_length=16,
        choices=ReferenceVisibility.choices,
        default=ReferenceVisibility.SHARED,
        verbose_name="النطاق",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="private_references",
        verbose_name="المالك",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="created_references",
        verbose_name="أنشأه",
    )
    is_active = models.BooleanField(default=True, verbose_name="نشط")
    deleted_at = models.DateTimeField(
        null=True, blank=True, verbose_name="تاريخ الحذف"
    )
    #: Bumped by the service layer whenever the tree changes. Informational; the
    #: frozen snapshot is what protects history, not this counter.
    revision = models.PositiveIntegerField(default=1, verbose_name="المراجعة")

    objects = ReferenceQuerySet.as_manager()

    class Meta:
        verbose_name = "مرجع تفتيش"
        verbose_name_plural = "مراجع التفتيش"
        ordering = ["title", "id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(visibility=ReferenceVisibility.SHARED)
                    | models.Q(owner__isnull=False)
                ),
                name="private_reference_requires_owner",
            )
        ]

    def __str__(self) -> str:
        return self.title

    @property
    def is_shared(self) -> bool:
        return self.visibility == ReferenceVisibility.SHARED

    @property
    def is_private(self) -> bool:
        return self.visibility == ReferenceVisibility.PRIVATE

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def root_nodes(self):
        return self.nodes.filter(parent__isnull=True)

    def ordered_nodes(self):
        """Whole tree in deterministic presentation order."""
        return self.nodes.select_related("parent").order_by(
            "position", "id"
        )

    def clean(self):
        if self.visibility == ReferenceVisibility.PRIVATE and self.owner_id is None:
            raise ValidationError(
                {"owner": "المرجع الخاص يجب أن يكون له مالك."}
            )


class ReferenceNode(TimeStampedModel):
    """A node in a reference tree: branch, description/specification or item.

    ``code`` is the stable identifier used to match this node across frozen
    snapshots, guides, assignment entries and exports. It is unique inside a
    reference and must not be recycled.
    """

    reference = models.ForeignKey(
        Reference,
        on_delete=models.CASCADE,
        related_name="nodes",
        verbose_name="المرجع",
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
    code = models.CharField(max_length=120, verbose_name="المعرّف الثابت")
    title = models.CharField(max_length=300, verbose_name="العنوان")
    body = models.TextField(blank=True, verbose_name="التفصيل")
    value_type = models.CharField(
        max_length=16,
        choices=ValueType.choices,
        default=ValueType.NONE,
        verbose_name="نوع القيمة",
    )
    is_mandatory = models.BooleanField(
        default=False,
        verbose_name="إلزامي في المرجع",
        help_text="يمثل قيدًا أساسيًا يورَّث إلى الزيارة كمتطلب إتمام.",
    )
    is_scope_locked = models.BooleanField(
        default=False,
        verbose_name="مقيد النطاق في المرجع",
        help_text="يمنع إخراج العنصر (أو أبنائه) من نطاق الزيارة.",
    )
    position = models.IntegerField(default=0, verbose_name="الترتيب")

    class Meta:
        verbose_name = "عنصر مرجع"
        verbose_name_plural = "عناصر المرجع"
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["reference", "code"], name="unique_node_code_per_reference"
            )
        ]
        indexes = [
            models.Index(fields=["reference", "parent", "position"]),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.title}"

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
    def carries_value(self) -> bool:
        return self.node_type == NodeType.DESCRIPTION and self.value_type != ValueType.NONE

    def clean(self):
        parent_type = self.parent.node_type if self.parent_id else None
        allowed = ALLOWED_PARENTS[self.node_type]
        if parent_type not in allowed:
            raise ValidationError(
                {
                    "parent": (
                        "تركيبة التسلسل غير مسموحة: "
                        f"{self.get_node_type_display()} لا يمكن أن يكون تحت "
                        f"{parent_type or 'الجذر'}."
                    )
                }
            )
        if self.parent_id and self.reference_id and self.parent.reference_id != self.reference_id:
            raise ValidationError(
                {"parent": "لا يمكن ربط عنصر بعقدة أم من مرجع آخر."}
            )
        if self.is_branch and self.value_type != ValueType.NONE:
            raise ValidationError(
                {"value_type": "الفرع لا يحمل قيمة."}
            )
        if self.is_checklist_item and self.value_type != ValueType.NONE:
            raise ValidationError(
                {"value_type": "بند التفتيش لا يحمل نوع قيمة؛ نتيجته تُسجَّل في الزيارة."}
            )


class SubmissionStatus(models.TextChoices):
    PENDING = "PENDING", "قيد المراجعة"
    APPROVED = "APPROVED", "مقبول"
    REJECTED = "REJECTED", "مرفوض"


class ReferenceSubmission(TimeStampedModel):
    """A frozen proposal asking to turn a private reference into a shared one.

    The snapshot is immutable: later edits to the private reference never change
    what the administrator is reviewing. When approved, a *new* shared reference
    is created; the private reference keeps its ownership and history.
    """

    reference = models.ForeignKey(
        Reference,
        on_delete=models.CASCADE,
        related_name="submissions",
        verbose_name="المرجع الخاص",
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="reference_submissions",
        verbose_name="قدّمه",
    )
    status = models.CharField(
        max_length=16,
        choices=SubmissionStatus.choices,
        default=SubmissionStatus.PENDING,
        verbose_name="الحالة",
    )
    note = models.TextField(blank=True, verbose_name="ملاحظة المفتش")
    snapshot = models.JSONField(verbose_name="لقطة المحتوى المقترح")

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_submissions",
        verbose_name="راجعه",
    )
    reviewed_at = models.DateTimeField(
        null=True, blank=True, verbose_name="تاريخ المراجعة"
    )
    review_note = models.TextField(blank=True, verbose_name="ملاحظة المراجعة")
    approved_reference = models.ForeignKey(
        Reference,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="approved_from_submission",
        verbose_name="المرجع المشترك الناتج",
    )

    class Meta:
        verbose_name = "مقترح تعميم مرجع"
        verbose_name_plural = "مقترحات تعميم المراجع"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"مقترح #{self.pk} — {self.reference.title}"

    @property
    def is_pending(self) -> bool:
        return self.status == SubmissionStatus.PENDING