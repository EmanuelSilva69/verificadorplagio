# Projeto de Analise Forense de Documentos

Aplicacao Streamlit para analise local de:
- Similaridade textual (plagio)
- Indicios de texto gerado por IA
- Validacao de referencias com APIs publicas e LLM local via Ollama

## Estrutura

- app/main.py: UI Streamlit (duas colunas)
- app/document_loader.py: leitura de PDF/DOCX/TXT e limpeza por paragrafos
- app/analysis_engine.py: plagio + heuristicas de IA
- app/reference_checker.py: extracao e validacao de referencias
- app/Dockerfile: imagem da aplicacao
- docker-compose.yml: app + servico Ollama

## Como executar

1. Na raiz do projeto, subir os containers:
   docker compose up --build -d

2. Baixar um modelo no Ollama (apenas na primeira vez):
   docker exec -it forensic-ollama ollama pull llama3.1:8b
   docker exec -it forensic-ollama ollama pull qwen2.5:latest

3. Acessar a interface:
   http://localhost:8501

## Observacoes

- O modulo de plagio executa busca profunda e scraping de resultados web usando provider configuravel (`searxng`, `serper` ou `tavily`).
- A validacao de referencias usa Crossref, Google Books e, quando disponivel, um LLM local no Ollama.
- O parecer de LLM e feito em conjunto por dois modelos (Llama 3.1 8B + Qwen 2.5), com consolidacao de consenso no relatorio.
- As marcacoes de texto seguem:
  - Amarelo: IA Provavel
  - Vermelho: Plagio Detectado
  - Azul: Referencia Duvidosa

## Configuracao de chaves (opcional)

No arquivo `.env` voce pode informar parametros para busca profunda:

- `SEARCH_API_PROVIDER=searxng|serper|tavily`
- `SEARCH_API_KEY` (obrigatorio para `serper` e `tavily`)
- `SEARCH_SEARXNG_URL` (obrigatorio para `searxng`)

Sem provider configurado, o sistema usa fallback do DuckDuckGo.

## Teste de integracao

Para validar busca web e resposta dos dois modelos no Ollama:

1. Ajuste `.env` com provider/chaves de busca e modelos Ollama.
2. Rode o script:
   python test_integration.py

Se tudo estiver correto, o script retorna status `OK` para Busca Web e Ollama Qwen+Llama.
