from django import forms

from .models import NodeType, Reference, ReferenceNode, ValueType


class ReferenceForm(forms.ModelForm):
    class Meta:
        model = Reference
        fields = ["title", "description"]
        labels = {"title": "العنوان", "description": "وصف مختصر"}


class ReferenceNodeForm(forms.ModelForm):
    class Meta:
        model = ReferenceNode
        fields = ["parent", "node_type", "code", "title", "body", "value_type",
                  "is_mandatory", "is_scope_locked", "position"]
        labels = {
            "parent": "العقدة الأم",
            "node_type": "النوع",
            "code": "المعرّف الثابت",
            "title": "العنوان",
            "body": "التفصيل",
            "value_type": "نوع القيمة",
            "is_mandatory": "متطلب إتمام أساسي",
            "is_scope_locked": "مقيد النطاق",
            "position": "الترتيب",
        }
        help_texts = {
            "code": "معرّف ثابت يُستخدم للمطابقة بين اللقطات والأدلة والتكليفات والتصدير. لا تُعد استخدامه.",
            "is_mandatory": "ينتقل إلى الزيارة كمتطلب إتمام.",
            "is_scope_locked": "يمنع إخراج العنصر وأبنائه من نطاق الزيارة.",
        }

    def __init__(self, *args, reference=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.reference = reference or getattr(self.instance, "reference", None)
        if self.reference is not None:
            queryset = self.reference.nodes.all()
            if self.instance.pk:
                queryset = queryset.exclude(pk=self.instance.pk)
            self.fields["parent"].queryset = queryset
        self.fields["parent"].required = False
        self.fields["parent"].empty_label = "— الجذر —"

    def clean(self):
        cleaned = super().clean()
        node_type = cleaned.get("node_type")
        value_type = cleaned.get("value_type", ValueType.NONE)
        parent = cleaned.get("parent")
        if node_type == NodeType.BRANCH and value_type != ValueType.NONE:
            self.add_error("value_type", "الفرع لا يحمل قيمة.")
        if node_type == NodeType.CHECKLIST_ITEM and value_type != ValueType.NONE:
            self.add_error("value_type", "بند التفتيش لا يحمل نوع قيمة.")
        if node_type == NodeType.CHECKLIST_ITEM and parent is None:
            self.add_error("parent", "بند التفتيش يجب أن يكون تحت فرع أو وصف.")
        if node_type == NodeType.BRANCH and parent is not None and parent.node_type != NodeType.BRANCH:
            self.add_error("parent", "الفرع يمكن أن يتبع فرعًا فقط.")
        if node_type == NodeType.DESCRIPTION and parent is not None and parent.node_type != NodeType.BRANCH:
            self.add_error("parent", "الوصف يمكن أن يتبع فرعًا فقط.")
        return cleaned

    def save(self, commit=True):
        node = super().save(commit=False)
        if self.reference is not None:
            node.reference = self.reference
        if commit:
            node.save()
        return node


class SubmissionForm(forms.Form):
    note = forms.CharField(
        label="ملاحظة للمراجعة",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="تُحفظ لقطة من المحتوى الحالي وقت الإرسال ولا تتأثر بالتعديلات اللاحقة.",
    )


class ReviewForm(forms.Form):
    note = forms.CharField(
        label="ملاحظة المراجعة",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )