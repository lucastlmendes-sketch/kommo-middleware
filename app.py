from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import os
import datetime
import requests

app = FastAPI(title="Kommo ↔ TecBrilho Middleware (Erika)")

# ---- CORS (opcional, mas ajuda em testes) ----
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- Variáveis de ambiente ----
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()

KOMMO_DOMAIN = (os.getenv("KOMMO_DOMAIN") or "").rstrip("/")  # ex.: https://tecbrilho.kommo.com
KOMMO_TOKEN = os.getenv("KOMMO_TOKEN", "").strip()

# Subdomínio permitido (opcional – segurança extra)
AUTHORIZED_SUBDOMAIN = None
if KOMMO_DOMAIN:
    # https://tecbrilho.kommo.com -> "tecbrilho"
    host = KOMMO_DOMAIN.split("//")[-1]
    AUTHORIZED_SUBDOMAIN = host.split(".")[0]

# Prompt principal da Erika.
# Se quiser, você pode mover esse texto para uma env var ERIKA_PROMPT
# e manter aqui apenas: ERIKA_PROMPT = os.getenv("ERIKA_PROMPT", "...")
ERIKA_PROMPT = """
Você é Erika, Agente Oficial da TecBrilho, especialista em estética automotiva,
vendedora consultiva, organizadora de agenda e relacionamento com clientes.
Fale sempre em português do Brasil, com mensagens curtas (1–2 frases),
em múltiplos turnos, usando o estilo e as regras definidas no script interno
da operação TecBrilho (vendas consultivas, foco na dor do cliente,
uso do catálogo TecBrilho como fonte oficial, regras comerciais e fluxo de funil
no Kommo). Nunca invente serviços, nomes ou valores.
Sempre peça nome e modelo do carro no início do atendimento e conduza o cliente
até o agendamento ou próximo passo adequado (reengajamento, pós-venda, etc.).
"""

if not OPENAI_API_KEY:
    # Sem chave não tem como subir o serviço corretamente
    raise RuntimeError("OPENAI_API_KEY não configurada no ambiente.")


# --------------------------------------------------------------------
# Chamada à OpenAI (Erika)
# --------------------------------------------------------------------
def call_openai_erika(user_message: str) -> str:
    """
    Envia a mensagem do cliente para a OpenAI usando o modelo configurado
    e o prompt da Erika. Usa a API /v1/responses.
    """
    url = "https://api.openai.com/v1/responses"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": OPENAI_MODEL,
        "input": [
            {"role": "system", "content": ERIKA_PROMPT},
            {"role": "user", "content": user_message},
        ],
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    # Formato atual da Responses API:
    # data["output"][0]["content"][0]["text"]["value"]
    try:
        text = (
            data["output"][0]["content"][0]["text"]["value"]
            .strip()
        )
    except Exception:
        # Se a estrutura mudar, devolvemos algo útil para depuração
        text = f"[ERRO AO LER RESPOSTA DA OPENAI] raw={data}"
    return text


# --------------------------------------------------------------------
# Endpoints básicos
# --------------------------------------------------------------------
@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "kommo-middleware",
        "time_utc": datetime.datetime.utcnow().isoformat(),
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


# --------------------------------------------------------------------
# Webhook do Kommo
# --------------------------------------------------------------------
@app.post("/kommo-webhook")
async def kommo_webhook(request: Request):
    payload = await request.json()
    print(f"{datetime.datetime.now()} Webhook recebido: keys={list(payload.keys())}")

    # 1) Validação opcional do subdomínio do Kommo
    try:
        account = payload.get("account") or payload.get("_embedded", {}).get("account") or {}
        subdomain = account.get("subdomain")
    except Exception:
        subdomain = None

    if AUTHORIZED_SUBDOMAIN and subdomain and subdomain != AUTHORIZED_SUBDOMAIN:
        raise HTTPException(
            status_code=401,
            detail=f"Subdomínio não autorizado: {subdomain}",
        )

    data = payload.get("data") or payload

    # 2) Extrai texto da mensagem
    message = (
        (data.get("message") or {}).get("text")
        or data.get("text")
        or (data.get("last_message") or {}).get("text")
        or ""
    )

    # 3) Extrai lead_id (formato mais comum dos webhooks do Kommo)
    lead = data.get("lead") or {}
    lead_id = lead.get("id") or data.get("lead_id")

    if not message or not str(message).strip():
        # Nada pra responder
        return {
            "status": "ignored",
            "reason": "sem mensagem",
            "payload_keys": list(payload.keys()),
        }

    # 4) Chama Erika (OpenAI)
    try:
        ai_response = call_openai_erika(str(message))
    except Exception as e:
        print("Erro ao chamar OpenAI:", e)
        raise HTTPException(status_code=500, detail=f"Erro ao chamar OpenAI: {e}")

    # 5) Cria nota no Kommo (se tivermos lead_id + config do Kommo)
    note_status = "skipped"
    if lead_id and KOMMO_DOMAIN and KOMMO_TOKEN:
        note_payload = [
            {
                "entity_id": lead_id,
                "note_type": "common",
                "params": {
                    "text": f"🤖 Erika: {ai_response}"
                },
            }
        ]

        try:
            notes_url = f"{KOMMO_DOMAIN}/api/v4/leads/notes"
            r = requests.post(
                notes_url,
                headers={"Authorization": f"Bearer {KOMMO_TOKEN}"},
                json=note_payload,
                timeout=30,
            )
            r.raise_for_status()
            note_status = "ok"
        except Exception as e:
            print("Erro ao criar nota no Kommo:", e)
            note_status = f"failed: {e}"

    return {
        "status": "ok",
        "lead_id": lead_id,
        "ai_response": ai_response,
        "kommo_note": note_status,
    }
