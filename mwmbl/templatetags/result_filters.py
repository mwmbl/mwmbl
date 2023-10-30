from django.template import Library
from django.utils.html import conditional_escape
from django.utils.safestring import mark_safe

register = Library()


@register.filter(needs_autoescape=True)
def strengthen(spans, autoescape=True):
    escape = conditional_escape if autoescape else lambda x: x
    strengthened = []
    for span in spans:
        escaped_value = escape(span["value"])
        if span["is_bold"]:
            strengthened.append(f"<strong>{escaped_value}</strong>")
        else:
            strengthened.append(escaped_value)
    return mark_safe("".join(strengthened))
