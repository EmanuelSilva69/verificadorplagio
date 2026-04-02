"""Interface Streamlit para analise forense com visualizacoes avancadas."""

from __future__ import annotations

import html
from typing import Dict, List

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from analysis_engine import analyze_document
from document_loader import load_document


COLOR_MAP = {
    "critical_plagiarism": "#ff6b6b",
    "suspected_ai": "#ffe066",
    "formatting_alert": "#ffd8a8",
    "inconsistent": "#ffb347",
    "reference": "#8ec5ff",
    "safe": "#90d7a1",
}

LABEL_META = {
    "critical_plagiarism": "Plágio Direto detectado via busca Web (Similaridade > 55%).",
    "suspected_ai": "Consenso de IA (Qwen e Llama marcam como provável IA).",
    "formatting_alert": "Alerta de Formatação: heurística rápida identificou artefatos típicos de LLM.",
    "inconsistent": "Divergência entre modelos (um IA e outro humano).",
    "reference": "Citação bibliográfica não encontrada ou potencial alucinação.",
    "safe": "Trecho classificado como original/humano por ambos os modelos.",
}

PRIORITY_ORDER = ["critical_plagiarism", "suspected_ai", "formatting_alert", "inconsistent", "reference", "safe"]


def _badge_html(label_key: str) -> str:
    return (
        "<span style='display:inline-block;padding:2px 8px;border-radius:999px;"
        f"background:{COLOR_MAP[label_key]};color:#1b1b1b;font-weight:600;font-size:12px;'>"
        f"{label_key.replace('_', ' ').title()}</span>"
    )


def _build_alert_labels(analysis: Dict[str, object], total_paragraphs: int) -> Dict[int, List[str]]:
    labels: Dict[int, List[str]] = {idx: [] for idx in range(total_paragraphs)}

    for hit in analysis.get("plagiarism_hits", []):
        idx = int(hit.get("paragraph_index", -1))
        if 0 <= idx < total_paragraphs and float(hit.get("similarity", 0.0)) > 0.55:
            labels[idx].append("critical_plagiarism")

    for row in analysis.get("ai_llm_scores", []):
        idx = int(row.get("paragraph_index", -1))
        if not (0 <= idx < total_paragraphs):
            continue
        qwen = float(row.get("qwen_probability", 0.0))
        llama = float(row.get("llama_probability", 0.0))
        if qwen >= 0.60 and llama >= 0.60:
            labels[idx].append("suspected_ai")
        elif (qwen >= 0.60 and llama <= 0.40) or (llama >= 0.60 and qwen <= 0.40):
            labels[idx].append("inconsistent")
        elif qwen <= 0.40 and llama <= 0.40:
            labels[idx].append("safe")

    for row in analysis.get("fast_heuristic_hits", []):
        idx = int(row.get("paragraph_index", -1))
        if 0 <= idx < total_paragraphs:
            labels[idx].append("formatting_alert")

    for ref in analysis.get("reference_checks", []):
        idx = int(ref.get("paragraph_index", -1))
        if 0 <= idx < total_paragraphs and ref.get("status") == "dubious":
            labels[idx].append("reference")
        if 0 <= idx < total_paragraphs and ref.get("status") == "ok":
            labels[idx].append("safe")

    for idx in range(total_paragraphs):
        deduped: List[str] = []
        seen = set()
        for label in labels[idx]:
            if label not in seen:
                seen.add(label)
                deduped.append(label)
        labels[idx] = deduped

    return labels


def _apply_reference_audit_override(
    labels_by_paragraph: Dict[int, List[str]],
    audit_results: List[Dict[str, object]],
) -> Dict[int, List[str]]:
    """Atualiza marcação para Referência Duvidosa quando auditoria confirmar alucinação."""
    merged = {idx: list(labels) for idx, labels in labels_by_paragraph.items()}

    for row in audit_results:
        if not bool(row.get("confirmed_hallucination", False)):
            continue
        idx = int(row.get("paragraph_index", -1))
        if idx < 0:
            continue
        merged.setdefault(idx, [])
        if "reference" not in merged[idx]:
            merged[idx].append("reference")

    return merged


def _render_detection_legend() -> None:
    with st.expander("📖 Entenda as Marcações", expanded=False):
        for label in PRIORITY_ORDER:
            st.markdown(f"{_badge_html(label)} {LABEL_META[label]}", unsafe_allow_html=True)


def _render_highlighted_text(paragraphs: List[str], labels_by_paragraph: Dict[int, List[str]]) -> str:
    rendered: List[str] = []
    for idx, paragraph in enumerate(paragraphs):
        escaped = html.escape(paragraph)
        labels = labels_by_paragraph.get(idx, [])

        color = "transparent"
        for key in PRIORITY_ORDER:
            if key in labels:
                color = COLOR_MAP[key]
                break

        badges = " ".join(_badge_html(key) for key in labels if key in COLOR_MAP)
        robot_icon = " 🤖" if ("critical_plagiarism" in labels and "suspected_ai" in labels) else ""

        style = (
            "padding: 10px; border-radius: 8px; margin-bottom: 8px;"
            f"background-color: {color}; border: 1px solid #d0d0d0;"
        )

        rendered.append(f"<div style='{style}'><div style='margin-bottom:6px'>{badges}{robot_icon}</div>{escaped}</div>")
    return "\n".join(rendered)


def _render_distribution_pie(analysis: Dict[str, object]) -> None:
    distribution = analysis.get("distribution", {})
    df = pd.DataFrame(
        {
            "Categoria": ["Texto Original", "IA", "Plagio Web"],
            "Valor": [
                int(distribution.get("original", 0)),
                int(distribution.get("ai", 0)),
                int(distribution.get("web_plagiarism", 0)),
            ],
        }
    )
    if float(df["Valor"].sum()) <= 0:
        st.info("Distribuicao indisponivel: documento sem dados suficientes para classificar.")
        return

    fig = px.pie(
        df,
        values="Valor",
        names="Categoria",
        hole=0.35,
        color="Categoria",
        color_discrete_map={
            "Texto Original": "#8fcf9c",
            "IA": "#fff3a3",
            "Plagio Web": "#ff9a9a",
        },
        title="Distribuicao Geral",
    )
    st.plotly_chart(fig, use_container_width=True)


def _render_radar_ai_aura(analysis: Dict[str, object]) -> None:
    ai_metrics = analysis.get("ai_metrics", {})
    baseline = analysis.get("human_baseline", {})

    categories = ["Uniformidade", "Repeticao Lexical", "Densidade de Conectivos"]
    user_values = [
        float(ai_metrics.get("uniformity", 0.0)),
        float(ai_metrics.get("repetition", 0.0)),
        float(ai_metrics.get("connector_ratio", 0.0)),
    ]
    human_values = [
        float(baseline.get("uniformidade", 0.0)),
        float(baseline.get("repeticao", 0.0)),
        float(baseline.get("conectivos", 0.0)),
    ]

    fig = go.Figure()
    fig.add_trace(
        go.Scatterpolar(
            r=user_values,
            theta=categories,
            fill="toself",
            name="Texto do Usuario",
            line_color="#ff9a9a",
        )
    )
    fig.add_trace(
        go.Scatterpolar(
            r=human_values,
            theta=categories,
            fill="toself",
            name="Media Humana",
            line_color="#7bb1ff",
        )
    )
    fig.update_layout(
        title="Aura da IA (Radar)",
        polar={"radialaxis": {"visible": True, "range": [0, 1]}},
        showlegend=True,
    )
    st.plotly_chart(fig, use_container_width=True)


def _render_stacked_ai_bars(analysis: Dict[str, object]) -> None:
    rows = analysis.get("ai_llm_scores", [])
    if not rows:
        st.info("Nao ha dados de IA por paragrafo para o grafico de barras.")
        return

    df = pd.DataFrame(rows)
    df["Paragrafo"] = df["paragraph_index"].apply(lambda x: f"P{x + 1}")
    melted = df.melt(
        id_vars=["Paragrafo"],
        value_vars=["qwen_probability", "llama_probability"],
        var_name="Modelo",
        value_name="Probabilidade",
    )
    melted["Modelo"] = melted["Modelo"].replace(
        {"qwen_probability": "Qwen", "llama_probability": "Llama"}
    )

    fig = px.bar(
        melted,
        x="Paragrafo",
        y="Probabilidade",
        color="Modelo",
        barmode="stack",
        title="Probabilidade de IA por Paragrafo (Qwen vs Llama)",
        color_discrete_map={"Qwen": "#f4a261", "Llama": "#2a9d8f"},
    )
    fig.update_yaxes(range=[0, 2])
    st.plotly_chart(fig, use_container_width=True)


def _render_similarity_heatmap(analysis: Dict[str, object]) -> None:
    heatmap = analysis.get("similarity_heatmap", {})
    matrix = heatmap.get("matrix", [])
    x_labels = heatmap.get("source_labels", [])
    y_labels = heatmap.get("paragraph_labels", [])

    if not matrix or not x_labels or not y_labels:
        st.info("Heatmap indisponivel: fontes web suficientes nao encontradas.")
        return

    fig = go.Figure(
        data=go.Heatmap(
            z=matrix,
            x=x_labels,
            y=y_labels,
            colorscale="YlOrRd",
            colorbar={"title": "Similaridade"},
        )
    )
    fig.update_layout(title="Heatmap de Similaridade (Documento vs Top 5 Fontes)")
    st.plotly_chart(fig, use_container_width=True)


def _build_side_by_side_table(analysis: Dict[str, object]) -> pd.DataFrame:
    rows: List[Dict[str, str]] = []
    for hit in analysis.get("plagiarism_hits", []):
        llm = hit.get("llm_consensus", {})
        qwen = llm.get("qwen", {})
        llama = llm.get("llama", {})
        combined = f"{qwen.get('veredito', 'n/a')} | {llama.get('veredito', 'n/a')} ({llm.get('confidence', 'Inconsistente')})"

        rows.append(
            {
                "Trecho Suspeito": str(hit.get("phrase", ""))[:220],
                "Texto Original Encontrado na Web": str(hit.get("scraped_text", "") or hit.get("source_excerpt", ""))[:280],
                "Fonte Web (URL)": str(hit.get("source_url", "")),
                "Veredito Combinado": combined,
            }
        )

    return pd.DataFrame(rows)


def _build_heuristic_export_df(analysis: Dict[str, object]) -> pd.DataFrame:
    rows: List[Dict[str, object]] = []
    for row in analysis.get("fast_heuristic_rows", []):
        rows.append(
            {
                "paragraph_index": int(row.get("paragraph_index", -1)) + 1,
                "heuristic_score": int(row.get("heuristic_score", 0)),
                "is_suspicious": bool(row.get("is_suspicious", False)),
                "critical_triggered": bool(row.get("critical_triggered", False)),
                "total_hits": int(row.get("total_hits", 0)),
                "density_per_100_words": float(row.get("density_per_100_words", 0.0)),
                "evidence": " | ".join(str(item) for item in row.get("evidence", [])),
            }
        )
    return pd.DataFrame(rows)


def _render_report_details(
    analysis: Dict[str, object],
    alert_labels_by_paragraph: Dict[int, List[str]],
) -> None:
    st.metric("Similaridade media (plagio web)", f"{analysis['plagiarism_percentage']:.2f}%")
    st.metric("Probabilidade heuristica de IA", f"{analysis['ai_probability']:.2f}%")

    heuristic_hits = analysis.get("fast_heuristic_hits", [])
    st.caption(
        f"Triagem rápida: {len(heuristic_hits)} parágrafo(s) com alerta de formatação por regex."
    )

    if heuristic_hits:
        with st.expander("Evidências da triagem heurística", expanded=False):
            for row in heuristic_hits:
                paragraph_number = int(row.get("paragraph_index", -1)) + 1
                score = int(row.get("heuristic_score", 0))
                st.markdown(f"- Parágrafo {paragraph_number} | Score heurístico: {score}")
                for item in row.get("evidence", []):
                    st.caption(f"• {item}")

    export_df = _build_heuristic_export_df(analysis)
    if not export_df.empty:
        st.download_button(
            label="Baixar CSV da triagem heurística",
            data=export_df.to_csv(index=False),
            file_name="triagem_heuristica.csv",
            mime="text/csv",
        )

    stats = analysis.get("search_stats", {})
    st.caption(
        "Busca web: "
        f"provider={stats.get('provider')} | "
        f"queries={stats.get('queries_executed')} | "
        f"resultados={stats.get('web_results_total')} | "
        f"paginas raspadas={stats.get('scraped_pages')}"
    )

    _render_detection_legend()

    st.markdown("### Evidencias de plagio")
    hits = analysis.get("plagiarism_hits", [])
    if not hits:
        st.write("Nenhuma similaridade acima do limiar configurado.")
    else:
        for hit in hits:
            paragraph_index = int(hit["paragraph_index"])
            badges = " ".join(
                _badge_html(label)
                for label in alert_labels_by_paragraph.get(paragraph_index, [])
                if label in COLOR_MAP
            )
            robot_icon = (
                " 🤖"
                if (
                    "critical_plagiarism" in alert_labels_by_paragraph.get(paragraph_index, [])
                    and "suspected_ai" in alert_labels_by_paragraph.get(paragraph_index, [])
                )
                else ""
            )

            st.markdown(
                f"- Paragrafo {hit['paragraph_index'] + 1}: {hit['classification']} | "
                f"similaridade={hit['similarity'] * 100:.1f}%"
            )
            if badges:
                st.markdown(f"{badges}{robot_icon}", unsafe_allow_html=True)
            st.caption(f"Fonte: {hit.get('source_title', '')} | {hit.get('source_url', '')}")

            llm = hit.get("llm_consensus", {})
            qwen = llm.get("qwen", {})
            llama = llm.get("llama", {})
            st.caption(
                f"Qwen: {qwen.get('veredito', qwen.get('error', 'n/a'))} | "
                f"{qwen.get('justificativa', '')}"
            )
            st.caption(
                f"Llama: {llama.get('veredito', llama.get('error', 'n/a'))} | "
                f"{llama.get('justificativa', '')}"
            )
            st.caption(f"Confianca: {llm.get('confidence', 'Inconsistente')}")

def _render_llm_verdict_tab(analysis: Dict[str, object]) -> None:
    deep_enabled = bool(analysis.get("deep_ai_enabled", False))
    if not deep_enabled:
        st.warning("Análise profunda desativada pelo usuário.")
        return

    st.markdown("### Veredito Qwen/Llama por Parágrafo")
    ai_rows = analysis.get("ai_llm_scores", [])
    if not ai_rows:
        st.info("Sem dados de veredito para exibir.")
        return

    for row in ai_rows:
        paragraph_number = int(row.get("paragraph_index", 0)) + 1
        st.markdown(f"- Parágrafo {paragraph_number}")
        st.caption(
            f"Qwen={float(row.get('qwen_probability', 0.0)):.2f} | "
            f"Llama={float(row.get('llama_probability', 0.0)):.2f} | "
            f"Consenso={float(row.get('consensus_probability', 0.0)):.2f}"
        )

        qwen_raw_response = str(row.get("qwen_raw_response", "")).strip()
        llama_raw_response = str(row.get("llama_raw_response", "")).strip()
        if qwen_raw_response:
            st.write(f"Qwen: {qwen_raw_response}")
        if llama_raw_response:
            st.write(f"Llama: {llama_raw_response}")


def _render_reference_card(row: Dict[str, object], title: str) -> None:
    reference = str(row.get("reference", ""))
    found_google = str(row.get("found_google", "Nao"))
    status_web = str(row.get("status_web", row.get("pipeline_status", "inconclusivo")))
    veredito_final = str(row.get("veredito_final", "inconclusivo"))
    top_source = str(row.get("top_source", ""))
    qwen = row.get("llm_consensus", {}).get("qwen", {}) if isinstance(row.get("llm_consensus", {}), dict) else {}
    llama = row.get("llm_consensus", {}).get("llama", {}) if isinstance(row.get("llm_consensus", {}), dict) else {}

    st.markdown(
        f"<div style='padding:12px;border:1px solid #d9d9d9;border-radius:10px;margin-bottom:10px;background:#fafafa;'>"
        f"<div style='font-weight:700;margin-bottom:6px;'>{title}</div>"
        f"<div style='margin-bottom:8px;'><strong>Referência:</strong> {html.escape(reference)}</div>"
        f"<div><strong>Web:</strong> {html.escape(status_web)} | <strong>Google:</strong> {html.escape(found_google)} | <strong>Fonte:</strong> {html.escape(top_source)}</div>"
        f"<div><strong>Qwen:</strong> {html.escape(str(qwen.get('veredito', qwen.get('error', 'n/a'))))} | {html.escape(str(qwen.get('justificativa', '')))}</div>"
        f"<div><strong>Llama:</strong> {html.escape(str(llama.get('veredito', llama.get('error', 'n/a'))))} | {html.escape(str(llama.get('justificativa', '')))}</div>"
        f"<div><strong>Veredito Final:</strong> {html.escape(veredito_final)}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )


def main() -> None:
    st.set_page_config(page_title="Forense de Plagio e IA", layout="wide")
    st.title("Analise Forense de Documentos")
    st.write("Upload de PDF/DOCX/TXT para detectar plagio web, IA e referencias duvidosas.")

    enable_deep_ai = st.toggle(
        "Habilitar Análise Profunda com IA (Llama/Qwen)",
        value=False,
        help="A análise profunda consome mais tempo e hardware. Use a análise rápida para uma triagem inicial.",
    )

    uploaded_file = st.file_uploader("Selecione um arquivo", type=["pdf", "docx", "doc", "txt"])
    if not uploaded_file:
        st.info("Envie um documento para iniciar.")
        return

    try:
        paragraphs, unified_text = load_document(uploaded_file.name, uploaded_file.getvalue())
    except Exception as exc:  # noqa: BLE001
        st.error(f"Falha no parsing do arquivo: {exc}")
        return

    if not paragraphs:
        st.warning("Nenhum texto util encontrado no documento.")
        return

    action_col1, action_col2 = st.columns(2)
    with action_col1:
        run_forensic = st.button("Executar analise forense", type="primary")
    with action_col2:
        run_aux_ai = st.button("Rodar analise auxiliar IA (Qwen + Llama 8.1)")

    if not run_forensic and not run_aux_ai:
        st.text_area("Texto extraido (pre-visualizacao)", unified_text, height=360)
        return

    deep_ai_for_run = enable_deep_ai or run_aux_ai
    if run_aux_ai and not enable_deep_ai:
        st.info("Análise auxiliar de IA acionada manualmente para esta execução.")

    progress_widget = st.progress(0, text="Preparando execucao...")
    debug_logs: List[str] = []
    with st.status("Iniciando Analise Forense...", expanded=True) as runtime_status:
        current_label = "Iniciando Analise Forense..."

        def _set_status_label(label: str) -> None:
            nonlocal current_label
            current_label = label
            runtime_status.update(label=current_label, state="running")

        def _status_callback(message: str) -> None:
            _set_status_label(message)

        def _progress_callback(value: float) -> None:
            clamped = max(0.0, min(1.0, float(value)))
            progress_widget.progress(int(clamped * 100), text=f"Progresso geral: {int(clamped * 100)}%")

        def _model_progress_callback(model_pos: int, model_name: str, done: int, total: int, section: str) -> None:
            pct = int((done / max(1, total)) * 100)
            blocks = int(pct / 10)
            bar = "█" * blocks + "░" * (10 - blocks)
            _set_status_label(
                f"🤖 [Modelo {model_pos}/2] {model_name} analisando {section}: [{bar}] {pct}%"
            )

        def _reference_progress_callback(done: int, total: int, phase: str) -> None:
            pct = int((done / max(1, total)) * 100)
            blocks = int(pct / 10)
            bar = "█" * blocks + "░" * (10 - blocks)
            phase_label = {
                "web": "Busca Web",
                "qwen": "Qwen (Bibliotecário)",
                "llama": "Llama (Contra-parecer)",
            }.get(phase, phase)
            _set_status_label(
                f"📚 Auditoria de Referências [{phase_label}]: [{bar}] {done}/{total}"
            )

        def _debug_callback(message: str) -> None:
            debug_logs.append(message)

        # Injeta callback de progresso sem alterar assinatura publica da funcao principal.
        setattr(_status_callback, "progress_callback", _progress_callback)
        setattr(_status_callback, "model_progress_callback", _model_progress_callback)
        setattr(_status_callback, "debug_callback", _debug_callback)
        setattr(_status_callback, "reference_progress_callback", _reference_progress_callback)

        analysis = analyze_document(
            paragraphs,
            status_callback=_status_callback,
            enable_deep_ai=deep_ai_for_run,
        )
        _progress_callback(1.0)
        runtime_status.update(label="Analise Forense Concluida", state="complete")

    if debug_logs:
        with st.expander("Log de depuracao", expanded=False):
            st.code("\n".join(debug_logs))

    alert_labels_by_paragraph = _build_alert_labels(analysis, len(paragraphs))

    audit_results: List[Dict[str, object]] = analysis.get("reference_checks", [])
    if audit_results:
        alert_labels_by_paragraph = _apply_reference_audit_override(alert_labels_by_paragraph, audit_results)

    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("Texto Extraido")
        st.text_area("Conteudo", unified_text, height=680)

    with col_right:
        tab_analise, tab_comparativo, tab_veredito, tab_auditoria = st.tabs(
            ["Relatorio de Analise", "Relatorio Comparativo", "Veredito Qwen/Llama", "🔍 Auditoria de Referências"]
        )

        with tab_analise:
            _render_report_details(analysis, alert_labels_by_paragraph)
            st.markdown("### Texto com marcacoes")
            highlighted = _render_highlighted_text(paragraphs, alert_labels_by_paragraph)
            st.markdown(highlighted, unsafe_allow_html=True)

        with tab_comparativo:
            st.markdown("### Tabela Comparativa Lado a Lado")
            table_df = _build_side_by_side_table(analysis)
            if not table_df.empty:
                st.dataframe(table_df, use_container_width=True)
            else:
                st.write("Sem trechos suspeitos para comparacao.")

            st.markdown("### Visualizacoes Avancadas")
            _render_distribution_pie(analysis)
            _render_radar_ai_aura(analysis)
            _render_stacked_ai_bars(analysis)
            _render_similarity_heatmap(analysis)

        with tab_veredito:
            _render_llm_verdict_tab(analysis)

        with tab_auditoria:
            st.markdown("### Protocolo de Verificacao Unitaria")

            if not analysis.get("reference_checks_preliminary") and not analysis.get("reference_checks"):
                st.info("Nao ha secao de referencias detectada para auditoria.")
            else:
                preliminary = analysis.get("reference_checks_preliminary", [])
                audit_results = analysis.get("reference_checks", [])

                if preliminary and not audit_results:
                    st.info("Auditando referencias em segundo plano...")
                    for idx, row in enumerate(preliminary):
                        _render_reference_card(row, f"Obra {idx + 1} (auditando)")

                if audit_results:
                    for idx, row in enumerate(audit_results):
                        _render_reference_card(row, f"Obra {idx + 1}")
                else:
                    st.write("Auditoria integrada ao pipeline principal. Aguarde a conclusao da analise.")


if __name__ == "__main__":
    main()
