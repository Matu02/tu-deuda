"""Gráficos interactivos; los importes visualizados siempre están en pesos."""

import pandas as pd
import plotly.express as px

COLORS = ["#0d9488", "#2563eb", "#f59e0b", "#8b5cf6", "#ef6461", "#06b6d4", "#64748b", "#ec4899", "#84cc16", "#6366f1"]


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
