
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
import requests, os

app = FastAPI(title="Kommo ↔ ChatGPT Middleware")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
KOMMO_TOKEN = os.getenv("KOMMO_TOKEN")
KOMMO_DOMAIN = os.getenv("KOMMO_DOMAIN")  # ex.: https://seusubdominio.kommo.com

@app.get("/")
def home():
    return {"status": "ok", "message": "Middleware ativo", "kommo_domain": KOMMO_DOMAIN}

@app.get("/health")
def health():
    ok = bool(OPENAI_API_KEY and KOMMO_TOKEN and KOMMO_DOMAIN)
    return {"ok": ok}

@app.post("/kommo-webhook")
async def kommo_webhook(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    message = (
        payload.get("message", {}).get("text")
        or payload.get("text")
        or payload.get("last_message", {}).get("text")
        or ""
    )
    lead = payload.get("lead") or {}
    lead_id = lead.get("id") or payload.get("lead_id")

    if not message:
        return {"status": "ignored", "reason": "no message in payload", "payload_keys": list(payload.keys())}

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    openai_body = {
        "model": "gpt-4o-mini",
        "messages": [
            {
                "role": "system",
                "content": (
                    "Você é o assistente de vendas do Lucas Mendes (TecBrilho). "
                    "Responda de forma cordial e objetiva, colete dados do cliente "
                    "(nome, telefone, serviço de interesse), registre próximos passos "
                    "e ofereça agendamento. Se houver objeções, contorne com empatia."
                ),
            },
            {"role": "user", "content": message},
        ],
    }

    try:
        ai = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=openai_body,
            timeout=30
        )
        ai.raise_for_status()
        resposta = ai.json()["choices"][0]["message"]["content"]
    except Exception as e:
        resposta = f"Erro ao gerar resposta: {e}"

    note_data = {"note_type": "common", "params": {"text": f"💬 ChatGPT: {resposta}"}}
    try:
        r = requests.post(
            f"{KOMMO_DOMAIN}/api/v4/leads/notes",
            headers={"Authorization": f"Bearer {KOMMO_TOKEN}"},
            json=note_data,
            timeout=30
        )
        r.raise_for_status()
    except Exception as e:
        return {"status": "error", "step": "kommo_note", "error": str(e), "ai_response": resposta}

    return {"status": "ok", "lead_id": lead_id, "ai_response": resposta}
