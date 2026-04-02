# Projeto de Análise Forense de Documentos

Aplicação Streamlit para análise local de documentos com foco em:
- similaridade textual e indícios de plágio;
- sinais de texto gerado por IA;
- validação de referências com APIs públicas e Ollama local.

## Como baixar e executar com Docker

1. Clone o repositório.
2. Na raiz do projeto, suba os containers:

```bash
docker compose up --build -d
```

3. Acesse a interface em:

```text
http://localhost:8501
```

4. Na primeira execução, baixe os modelos do Ollama:

```bash
docker exec -it forensic-ollama ollama pull llama3.1:8b
docker exec -it forensic-ollama ollama pull qwen2.5:latest
```

Se quiser acompanhar os logs:

```bash
docker compose logs -f
```

## O que o app faz

- Detecta trechos com risco de plágio usando busca web e comparação de similaridade.
- Faz análise de IA com dois modelos locais no Ollama, usando consenso entre Qwen e Llama.
- Gera métricas visuais com distribuição geral, radar de IA, barras por parágrafo e heatmap de similaridade.
- Exibe evidências por parágrafo com marcações coloridas e veredito combinado dos modelos.
- Faz auditoria de referências e sinaliza citações duvidosas.
- Permite baixar um CSV com a triagem heurística.

## Funcionalidades atuais

- Triagem heurística rápida por regex para reduzir falsos positivos e acelerar a análise.
- Scraper híbrido com `aiohttp` + `BeautifulSoup` e fallback para Selenium.
- Busca web configurável por provider.
- Consenso de IA com Qwen 2.5 e Llama 3.1.
- Interface preparada para uso em container, sem depender de volume bind no runtime.

## Configuração opcional com `.env`

O Docker já sobe com valores padrão, mas você pode criar um `.env` local para sobrescrever opções de busca:

- `SEARCH_API_PROVIDER=searxng|serper|tavily`
- `SEARCH_API_KEY` para `serper` e `tavily`
- `SEARCH_SEARXNG_URL` se quiser apontar para uma instância diferente

O arquivo `.env` não deve ser enviado para o GitHub. Se quiser documentar os campos, use um `.env.example`.

## Estrutura principal

- `app/main.py`: interface Streamlit e visualizações
- `app/analysis_engine.py`: motor de análise de plágio e IA
- `app/reference_checker.py`: validação e auditoria de referências
- `app/web_scraper.py`: captura de conteúdo web com fallback
- `app/Dockerfile`: imagem da aplicação
- `docker-compose.yml`: orquestra app, Ollama e SearXNG

## Teste de integração

Para validar a integração com busca web e Ollama:

```bash
python test_integration.py
```

Se tudo estiver correto, o script retorna `OK` para busca web e para os modelos Qwen + Llama.
