from django import forms

from visits.models import VisitNode

from .models import Guide


class GuideForm(forms.ModelForm):
    class Meta:
        model = Guide
        fields = ["title", "description", "status"]
        labels = {
            "title": "عنوان الدليل",
            "description": "وصف الدليل",
            "status": "الحالة",
        }
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}


class GuideTargetForm(forms.Form):
    code = forms.CharField(
        label="معرّف العنصر في المرجع",
        max_length=120,
        help_text="المعرّف الثابت للعنصر داخل المرجع المرتبط بالدليل.",
    )


class AssignmentCreateForm(forms.Form):
    """Creates the draft container; entries are added separately."""

    title = forms.CharField(label="عنوان التكليف", max_length=250)
    instruction = forms.CharField(
        label="نص التكليف",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
    )


class AssignmentEntryForm(forms.Form):
    target_node = forms.ModelChoiceField(
        queryset=VisitNode.objects.none(),
        label="العنصر المستهدف (داخل اللقطة المجمّدة)",
    )
    scope_locked = forms.BooleanField(label="قيد النطاق", required=False)
    completion_required = forms.BooleanField(label="متطلب إتمام", required=False)
    note = forms.CharField(
        label="ملاحظة", required=False, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args, visit=None, **kwargs):
        super().__init__(*args, **kwargs)
        if visit is not None:
            self.fields["target_node"].queryset = visit.nodes.order_by("position", "id")

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("scope_locked") and not cleaned.get("completion_required"):
            raise forms.ValidationError(
                "على بند التكليف أن يفرض قيد نطاق أو متطلب إتمام على الأقل."
            )
        return cleaned


class RevokeForm(forms.Form):
    reason = forms.CharField(
        label="سبب الإلغاء",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="سبب الإلغاء مطلوب ويُسجَّل في سجل التدقيق مع هوية الملغي وتاريخه.",
    )