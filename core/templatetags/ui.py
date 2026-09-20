"""Presentation helpers.

Arabic UI shows dates as dd/mm/yyyy. ``mm/dd/yyyy`` is never used, and the
date inputs are native pickers so the format is unambiguous at entry time.
"""

from django import template
from django.utils import timezone

from visits.models import ItemResult, NodeStatus, Origin

register = template.Library()

# ``strftime`` directives: Arabic UI shows dd/mm/yyyy, never mm/dd/yyyy.
_DATE_FORMAT = "%d/%m/%Y"


def _local(value):
    """Convert an aware datetime to local time; leave dates and naive values."""
    if hasattr(value, "utcoffset"):
        try:
            if value.utcoffset() is not None:
                return timezone.localtime(value)
        except (AttributeError, ValueError):
            return value
    return value


@register.filter
def ardate(value) -> str:
    if not value:
        return "—"
    if isinstance(value, str):
        return value
    return _local(value).strftime(_DATE_FORMAT)


@register.filter
def ardatetime(value) -> str:
    if not value:
        return "—"
    return _local(value).strftime("%d/%m/%Y %H:%M")


@register.filter
def indent(depth) -> str:
    return "— " * int(depth or 0)


@register.filter
def origin_label(value) -> str:
    if not value:
        return "سياق"
    return dict(Origin.choices).get(value, value)


@register.filter
def origin_badge(value) -> str:
    return {
        Origin.LEGACY: "badge-muted",
        Origin.MANUAL: "badge-info",
        Origin.GUIDE: "badge-ok",
        Origin.ASSIGNMENT: "badge-warn",
        Origin.LOCAL: "badge-info",
    }.get(value, "badge-muted")


@register.filter
def result_label(value) -> str:
    return dict(ItemResult.choices).get(value or "", "بدون نتيجة")


@register.filter
def result_badge(value) -> str:
    return {
        ItemResult.MATCH: "badge-ok",
        ItemResult.NOT_MATCH: "badge-danger",
        ItemResult.NOT_APPLICABLE: "badge-muted",
    }.get(value or "", "badge-draft")


@register.filter
def node_type_label(value) -> str:
    from catalog.models import NodeType

    return dict(NodeType.choices).get(value, value)


@register.filter
def status_label(value) -> str:
    return dict(NodeStatus.choices).get(value, value)


@register.filter
def value_display(node) -> str:
    """Readable rendering of a snapshot value node."""
    from catalog.models import ValueType

    if not node or node.value_type == ValueType.NONE:
        return "—"
    if node.value_type == ValueType.TEXT:
        return node.value_text or "—"
    if node.value_type == ValueType.NUMBER:
        if node.value_number is None:
            return "—"
        # Drop trailing zeros so 42.000 renders as 42.
        return format(node.value_number.normalize(), "f")
    if node.value_type == ValueType.DATE:
        return node.value_date.strftime(_DATE_FORMAT) if node.value_date else "—"
    if node.value_type == ValueType.BOOLEAN:
        if node.value_boolean is None:
            return "—"
        return "نعم" if node.value_boolean else "لا"
    return "—"