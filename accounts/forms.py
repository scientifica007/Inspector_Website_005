from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm

from .models import Role, User


class LoginForm(AuthenticationForm):
    username = forms.CharField(
        label="اسم المستخدم",
        widget=forms.TextInput(attrs={"autocomplete": "username", "autofocus": True}),
    )
    password = forms.CharField(
        label="كلمة المرور",
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )


class UserForm(forms.ModelForm):
    password1 = forms.CharField(
        label="كلمة المرور", widget=forms.PasswordInput, required=False,
        help_text="اتركها فارغة للإبقاء على كلمة المرور الحالية عند التعديل.",
    )
    password2 = forms.CharField(
        label="تأكيد كلمة المرور", widget=forms.PasswordInput, required=False
    )

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "display_name", "email", "role"]
        labels = {
            "username": "اسم المستخدم",
            "first_name": "الاسم",
            "last_name": "اللقب",
            "display_name": "الاسم الظاهر",
            "email": "البريد الإلكتروني",
            "role": "الدور",
        }

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get("password1"), cleaned.get("password2")
        if p1 or p2:
            if p1 != p2:
                self.add_error("password2", "كلمتا المرور غير متطابقتين.")
            elif len(p1) < 8:
                self.add_error("password1", "كلمة المرور قصيرة جدًا (8 أحرف على الأقل).")
        elif not self.instance.pk:
            self.add_error("password1", "كلمة المرور مطلوبة لإنشاء مستخدم جديد.")
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        password = self.cleaned_data.get("password1")
        if password:
            user.set_password(password)
        if commit:
            user.save()
        return user


class RegisterForm(UserCreationForm):
    """Self-service registration for inspectors.

    Only the inspector role can be self-selected; administrator accounts are
    created by an existing administrator.
    """

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "display_name", "email"]
        labels = {
            "username": "اسم المستخدم",
            "first_name": "الاسم",
            "last_name": "اللقب",
            "display_name": "الاسم الظاهر",
            "email": "البريد الإلكتروني",
        }

    def save(self, commit=True):
        user = super().save(commit=False)
        user.role = Role.INSPECTOR
        if commit:
            user.save()
        return user