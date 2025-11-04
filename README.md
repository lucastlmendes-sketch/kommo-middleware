
# Kommo ↔ ChatGPT Middleware (Render)

Este projeto conecta o Kommo ao ChatGPT via API. Ele recebe webhooks do Kommo e grava respostas da IA como notas no lead.

## 🚀 Deploy (via GitHub + Render)

1. Crie um repositório no GitHub chamado `kommo-middleware`.
2. Faça upload dos arquivos `app.py`, `requirements.txt`, `render.yaml`, `README.md`.
3. No Render, clique em **New → Web Service → Git Provider** e conecte ao repositório.
4. Confirme:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn app:app --host 0.0.0.0 --port 10000`
5. Em **Environment Variables**, adicione:
   - `OPENAI_API_KEY`
   - `KOMMO_TOKEN`
   - `KOMMO_DOMAIN`
6. Deploy. A URL pública será algo como `https://kommo-middleware.onrender.com`.
7. Use essa URL como **Middleware URL** no Kommo.
