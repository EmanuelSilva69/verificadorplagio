"""Script Streamlit para validar renderizacao dos graficos com dados ficticios.

Execucao:
    streamlit run test_visuals.py
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st


def main() -> None:
    st.set_page_config(page_title="Teste de Visuals", layout="wide")
    st.title("Teste de Renderizacao de Graficos")

    st.subheader("1) Radar - Aura da IA")
    radar_fig = go.Figure()
    radar_fig.add_trace(
        go.Scatterpolar(
            r=[0.72, 0.58, 0.61],
            theta=["Uniformidade", "Repeticao Lexical", "Densidade de Conectivos"],
            fill="toself",
            name="Texto Usuario",
        )
    )
    radar_fig.add_trace(
        go.Scatterpolar(
            r=[0.42, 0.27, 0.22],
            theta=["Uniformidade", "Repeticao Lexical", "Densidade de Conectivos"],
            fill="toself",
            name="Media Humana",
        )
    )
    radar_fig.update_layout(polar={"radialaxis": {"visible": True, "range": [0, 1]}})
    st.plotly_chart(radar_fig, use_container_width=True)

    st.subheader("2) Barras Empilhadas - IA por Paragrafo")
    bars_df = pd.DataFrame(
        {
            "Paragrafo": ["P1", "P2", "P3", "P4"],
            "Qwen": [0.65, 0.25, 0.78, 0.44],
            "Llama": [0.58, 0.31, 0.73, 0.49],
        }
    )
    bars_melted = bars_df.melt(id_vars=["Paragrafo"], var_name="Modelo", value_name="Probabilidade")
    bars_fig = px.bar(
        bars_melted,
        x="Paragrafo",
        y="Probabilidade",
        color="Modelo",
        barmode="stack",
        title="Probabilidade IA - Qwen vs Llama",
    )
    st.plotly_chart(bars_fig, use_container_width=True)

    st.subheader("3) Heatmap de Similaridade")
    heatmap_fig = go.Figure(
        data=go.Heatmap(
            z=[
                [0.12, 0.44, 0.67, 0.21, 0.53],
                [0.09, 0.18, 0.29, 0.61, 0.72],
                [0.33, 0.56, 0.77, 0.43, 0.26],
            ],
            x=["Fonte 1", "Fonte 2", "Fonte 3", "Fonte 4", "Fonte 5"],
            y=["P1", "P2", "P3"],
            colorscale="YlOrRd",
            colorbar={"title": "Similaridade"},
        )
    )
    heatmap_fig.update_layout(title="Heatmap Documento x Fontes")
    st.plotly_chart(heatmap_fig, use_container_width=True)

    st.success("Todos os graficos foram gerados com dados ficticios.")


if __name__ == "__main__":
    main()
