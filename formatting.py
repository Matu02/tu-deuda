"""Formato argentino de importes sin modificar los valores numéricos."""


def money(value):
    return "$ " + f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
