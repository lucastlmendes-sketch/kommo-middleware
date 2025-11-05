
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import requests, os, datetime

app = FastAPI(title="Kommo ↔ ChatGPT Middleware (Auto Secure Edition)")

# Variáveis de ambiente
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
KOMMO_TOKEN = os.getenv("KOMMO_TOKEN")
KOMMO_DOMAIN = os.getenv("KOMMO_DOMAIN")  # Ex: https://tecnobrilho.kommo.com
AUTHORIZED_SUBDOMAIN = KOMMO_DOMAIN.replace("https://", "").replace(".kommo.com", "") if KOMMO_DOMAIN else None

# CORS restrito — apenas o domínio do Kommo pode acessar
app.add_middleware(
    CORSMiddleware,
    allow_origins=[KOMMO_DOMAIN] if KOMMO_DOMAIN else ["*"],
    allow_credentials=True,
    allow_methods=["POST", "GET"],
    allow_headers=["Authorization", "Content-Type"],
)

@app.get("/")
def home():
    return {"status": "ok", "message": "Middleware ativo com validação automática de domínio", "kommo_domain": KOMMO_DOMAIN}

@app.get("/health")
def health():
    ok = bool(OPENAI_API_KEY and KOMMO_TOKEN and KOMMO_DOMAIN)
    return {"ok": ok, "timestamp": datetime.datetime.now().isoformat()}

@app.get("/ping")
def ping():
    return {"status": "alive", "timestamp": datetime.datetime.now().isoformat()}

@app.post("/kommo-webhook")
async def kommo_webhook(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Payload inválido ou ausente")

    # Validação automática do subdomínio do Kommo
    subdomain = payload.get("account", {}).get("subdomain")
    if not subdomain or subdomain != AUTHORIZED_SUBDOMAIN:
        raise HTTPException(status_code=401, detail=f"Subdomínio não autorizado: {subdomain}")

    print(f"[{datetime.datetime.now()}] Webhook recebido do subdomínio autorizado: {subdomain}")

    message = (
        payload.get("message", {}).get("text")
        or payload.get("text")
        or payload.get("last_message", {}).get("text")
        or ""
    )
    lead = payload.get("lead") or {}
    lead_id = lead.get("id") or payload.get("lead_id")

    if not message:
        return {"status": "ignored", "reason": "sem mensagem detectada", "payload_keys": list(payload.keys())}

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    openai_body = {
        "model": "gpt-4o-mini",
        "messages": [
            {
                "role": "system",
                "content": (
                    "Você é o assistente de vendas do Lucas Mendes (TecBrilho). "
                    "Responda de forma cordial, profissional e empática. "
                    "Colete nome, telefone e serviço de interesse; ofereça agendamento e contorne objeções com gentileza."
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
        return {"status": "erro", "etapa": "kommo_note", "erro": str(e), "resposta": resposta}

    return {"status": "ok", "lead_id": lead_id, "resposta": resposta}
