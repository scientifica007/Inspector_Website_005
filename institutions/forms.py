from django import forms

from .models import Institution


class InstitutionForm(forms.ModelForm):
    class Meta:
        model = Institution
        fields = ["name", "code", "kind", "location", "is_active"]
        labels = {
            "name": "الاسم",
            "code": "الرمز التعريفي",
            "kind": "النوع",
            "location": "الموقع",
            "is_active": "نشطة",
        }
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "اسم المؤسسة"}),
        }