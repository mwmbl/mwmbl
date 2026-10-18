from django import template

register = template.Library()


@register.filter
def minor_units(amount: int, symbol: str) -> str:
    """Format an amount held in cents or pence, e.g. 2500 with "$" as "$25.00"."""
    major, minor = divmod(amount, 100)
    return f"{symbol}{major:,}.{minor:02d}"
