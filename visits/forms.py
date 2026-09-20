from django import forms
from django.utils import timezone

from catalog.models import Reference, ReferenceVisibility, ValueType
from institutions.models import Institution

from .models import ItemResult, NodeType, Visit, VisitNode


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, attrs=None, format=None):
        super().__init__(attrs=attrs, format=format)


class VisitCreateForm(forms.Form):
    institution = forms.ModelChoiceField(
        queryset=Institution.objects.filter(is_active=True),
        label="المؤسسة",
        empty_label="— اختر مؤسسة —",
    )
    title = forms.CharField(
        label="عنوان الزيارة", required=False, max_length=250,
        help_text="اتركه فارغًا لاستخدام اسم المؤسسة.",
    )
    visit_date = forms.DateField(
        label="تاريخ الزيارة",
        widget=DateInput(format="%Y-%m-%d"),
        input_formats=["%Y-%m-%d", "%d/%m/%Y"],
        initial=timezone.localdate,
    )
    reference = forms.ModelChoiceField(
        queryset=Reference.objects.none(),
        label="المرجع",
        required=False,
        empty_label="— بدون مرجع (زيارة حرة البنية) —",
        help_text="الزيارة تحصل على لقطة مجمّدة من المرجع، ولا تتأثر بتغيّره لاحقًا.",
    )
    notes = forms.CharField(
        label="ملاحظات عامة", required=False, widget=forms.Textarea(attrs={"rows": 3})
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user is not None:
            self.fields["reference"].queryset = (
                Reference.objects.visible_to(user)
                .filter(deleted_at__isnull=True, is_active=True)
                .order_by("title")
            )


class LocalNodeForm(forms.Form):
    node_type = forms.ChoiceField(
        label="النوع",
        choices=[
            (NodeType.BRANCH, "فرع محلي"),
            (NodeType.DESCRIPTION, "وصف / مواصفة محلية"),
            (NodeType.CHECKLIST_ITEM, "بند تفتيش محلي"),
        ],
    )
    parent = forms.ModelChoiceField(
        queryset=VisitNode.objects.none(),
        label="العقدة الأم",
        required=False,
        empty_label="— الجذر —",
    )
    title = forms.CharField(label="العنوان", max_length=300)
    body = forms.CharField(
        label="التفصيل", required=False, widget=forms.Textarea(attrs={"rows": 3})
    )
    value_type = forms.ChoiceField(
        label="نوع القيمة",
        choices=ValueType.choices,
        required=False,
        initial=ValueType.NONE,
        help_text="يُستخدم مع الوصف / المواصفة.",
    )

    def __init__(self, *args, visit=None, **kwargs):
        super().__init__(*args, **kwargs)
        if visit is not None:
            self.fields["parent"].queryset = visit.nodes.filter(
                node_type__in=[NodeType.BRANCH, NodeType.DESCRIPTION]
            ).order_by("position", "id")

    def clean(self):
        cleaned = super().clean()
        node_type = cleaned.get("node_type")
        parent = cleaned.get("parent")
        value_type = cleaned.get("value_type") or ValueType.NONE
        if node_type == NodeType.BRANCH:
            if parent is not None and not parent.is_branch:
                self.add_error("parent", "الفرع يمكن أن يتبع فرعًا فقط.")
        if node_type == NodeType.CHECKLIST_ITEM:
            if parent is None:
                self.add_error("parent", "بند التفتيش يجب أن يتبع فرعًا أو وصفًا.")
            elif parent.is_checklist_item:
                self.add_error("parent", "لا يمكن إضافة بند تحت بند.")
        if node_type == NodeType.DESCRIPTION:
            if parent is not None and parent.is_checklist_item:
                self.add_error("parent", "لا يمكن إضافة وصف تحت بند.")
        return cleaned


class ItemResultForm(forms.Form):
    result = forms.ChoiceField(
        label="النتيجة",
        choices=[
            (ItemResult.MATCH, "مطابق"),
            (ItemResult.NOT_MATCH, "غير مطابق"),
            (ItemResult.NOT_APPLICABLE, "غير منطبق"),
        ],
        required=False,
    )
    observation = forms.CharField(
        label="ملاحظة / مشاهدة",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )


class ValueForm(forms.Form):
    value_text = forms.CharField(label="القيمة النصية", required=False,
                                 widget=forms.Textarea(attrs={"rows": 2}))
    value_number = forms.DecimalField(label="القيمة العددية", required=False)
    value_date = forms.DateField(
        label="القيمة (تاريخ)", required=False, widget=DateInput(format="%Y-%m-%d"),
        input_formats=["%Y-%m-%d", "%d/%m/%Y"],
    )
    value_boolean = forms.NullBooleanField(label="القيمة المنطقية", required=False)
    observation = forms.CharField(
        label="ملاحظة", required=False, widget=forms.Textarea(attrs={"rows": 2})
    )


class VisitNotesForm(forms.ModelForm):
    class Meta:
        model = Visit
        fields = ["title", "notes"]
        labels = {
            "title": "عنوان الزيارة",
            "notes": "ملاحظات عامة",
        }
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}