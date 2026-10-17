from markupsafe import Markup, escape

ALLOWED_ICONS = {"arrow-left", "arrow-right", "arrow-up-right", "calendar", "camera", "check", "chevron-left", "chevron-right", "clipboard", "coins", "copy", "download", "excel", "external-link", "file", "gift", "key", "keyboard", "logout", "mail", "menu", "pause", "pdf", "phone", "play", "plus", "qr", "receipt", "search", "settings", "shield", "sliders", "stamp", "store", "trash", "user", "user-plus", "users", "wallet", "x"}

def icon(name, class_name="icon"):
    if name not in ALLOWED_ICONS:
        raise ValueError(f"Icono no permitido: {name}")
    return Markup(f'<svg class="{escape(class_name)}" aria-hidden="true" focusable="false"><use href="/static/icons.svg#{escape(name)}"></use></svg>')


def contrast_text_color(color):
    """Return an accessible black/white foreground without altering brand data."""
    value = str(color or "").lstrip("#")
    if len(value) != 6:
        return "#FFFFFF"
    try:
        red, green, blue = (int(value[index:index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        return "#FFFFFF"
    luminance = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255
    return "#0F172A" if luminance > 0.55 else "#FFFFFF"
