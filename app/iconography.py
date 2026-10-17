from markupsafe import Markup, escape

ALLOWED_ICONS = {"arrow-left", "arrow-right", "arrow-up-right", "calendar", "camera", "check", "chevron-left", "chevron-right", "clipboard", "coins", "copy", "download", "excel", "external-link", "file", "gift", "key", "keyboard", "logout", "mail", "menu", "pause", "pdf", "phone", "play", "plus", "qr", "receipt", "search", "settings", "shield", "sliders", "stamp", "store", "trash", "user", "user-plus", "users", "wallet", "x"}

def icon(name, class_name="icon"):
    if name not in ALLOWED_ICONS:
        raise ValueError(f"Icono no permitido: {name}")
    return Markup(f'<svg class="{escape(class_name)}" aria-hidden="true" focusable="false"><use href="/static/icons.svg#{escape(name)}"></use></svg>')
