"""Gráficos interactivos; los importes visualizados siempre están en pesos."""

import pandas as pd
import plotly.express as px

COLORS = ["#0d9488", "#2563eb", "#f59e0b", "#8b5cf6", "#ef6461", "#06b6d4", "#64748b", "#ec4899", "#84cc16", "#6366f1"]
SITUATIONS = {
    1: "1 · Normal",
    2: "2 · Seguimiento especial / Riesgo bajo",
    3: "3 · Con problemas / Riesgo medio",
    4: "4 · Alto riesgo de insolvencia / Riesgo alto",
    5: "5 · Irrecuperable",
}
SITUATION_COLORS = dict(zip(SITUATIONS.values(), ["#0d9488", "#eab308", "#f97316", "#dc2626", "#881337"]))


def situation_chart(frame: pd.DataFrame):
    """Sumar montos de todas las entidades por mes y situación, sin perder nulos."""
    data = frame.copy()
    codes = pd.to_numeric(data.get("situacion", pd.Series(index=data.index, dtype="float64")), errors="coerce")
    data["Situación"] = codes.map(SITUATIONS).fillna("Sin clasificación (otra o no informada)")
    data = data.groupby(["periodo", "Situación"], as_index=False)["monto_ars"].sum()
    data["Mes"] = data["periodo"].map(lambda p: f"{p[:4]}-{p[4:]}")
    fig = px.bar(data, x="Mes", y="monto_ars", color="Situación", barmode="stack",
                 color_discrete_map={**SITUATION_COLORS, "Sin clasificación (otra o no informada)": "#64748b"},
                 category_orders={"Mes": sorted(data["Mes"].unique()), "Situación": list(SITUATIONS.values()) + ["Sin clasificación (otra o no informada)"]},
                 labels={"monto_ars": "Deuda (ARS)"})
    fig.update_traces(hovertemplate="%{x}<br>%{fullData.name}<br>ARS %{y:,.2f}<extra></extra>")
    fig.update_layout(height=500, separators=",.", legend=dict(orientation="h", y=-0.25),
                      margin=dict(t=20, b=20), xaxis=dict(type="category", title=None),
                      yaxis=dict(title="Deuda en pesos argentinos", rangemode="tozero"))
    return fig


def debt_chart(frame: pd.DataFrame):
    data = frame.groupby(["periodo", "entidad"], as_index=False)["monto_ars"].sum()
    data["Mes"] = data["periodo"].map(lambda p: f"{p[:4]}-{p[4:]}")
    fig = px.bar(data, x="Mes", y="monto_ars", color="entidad", barmode="stack",
                 color_discrete_sequence=COLORS,
                 labels={"monto_ars": "Deuda (ARS)", "entidad": "Entidad financiera"},
                 category_orders={"Mes": sorted(data["Mes"].unique())})
    fig.update_traces(hovertemplate="%{x}<br>%{fullData.name}<br>ARS %{y:,.2f}<extra></extra>")
    fig.update_layout(height=530, separators=",.", legend=dict(orientation="h", y=-0.25),
                      margin=dict(t=20, b=20), xaxis=dict(type="category", title=None),
                      yaxis=dict(title="Deuda en pesos argentinos", rangemode="tozero"))
    return fig


def checks_chart(frame: pd.DataFrame):
    data = frame.assign(monto=pd.to_numeric(frame["monto"], errors="raise")).groupby("causal", as_index=False, dropna=False)["monto"].sum()
    fig = px.pie(data, names="causal", values="monto", color_discrete_sequence=COLORS)
    fig.update_traces(textinfo="percent+label", hovertemplate="%{label}<br>ARS %{value:,.2f}<br>%{percent}<extra></extra>")
    fig.update_layout(height=400, separators=",.", margin=dict(t=20, b=20), legend_title_text="Causal")
    return fig


def companies_chart(frame: pd.DataFrame):
    data = frame.groupby(["CUIT", "razon_social"], as_index=False, dropna=False)["monto"].sum()
    data = data.sort_values(["monto", "CUIT"], ascending=[False, True])
    top = data.head(10).copy()
    top["Empresa"] = top["razon_social"].fillna("Sin razón social") + " · " + top["CUIT"].astype(str)
    values = top[["Empresa", "monto"]]
    if len(data) > 10:
        values = pd.concat([values, pd.DataFrame([{"Empresa": "Resto de empresas", "monto": data.iloc[10:]["monto"].sum()}])], ignore_index=True)
    fig = px.pie(values, names="Empresa", values="monto", color_discrete_sequence=COLORS)
    fig.update_traces(textinfo="percent", hovertemplate="%{label}<br>ARS %{value:,.2f}<br>%{percent}<extra></extra>")
    fig.update_layout(height=470, separators=",.", margin=dict(t=20, b=20), legend=dict(orientation="h", y=-0.1))
    return fig
