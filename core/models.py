from django.db import models


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="أُنشئ في")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="حُدّث في")

    class Meta:
        abstract = True