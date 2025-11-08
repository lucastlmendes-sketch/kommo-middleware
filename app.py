import os
import datetime
import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from openai import OpenAI

# Carrega variáveis de ambiente
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_ASSISTANT_ID = os.getenv("OPENAI_ASSISTANT_ID")  # ID da Erika
KOMMO_DOMAIN = (os.getenv("KOMMO_DOMAIN") or "").rstrip("/")
KOMMO_TOKEN = os.getenv("KOMMO_TOKEN") or ""
AUTHORIZED_SUBDOMAIN = None

if KOMMO_DOMAIN:
    host = KOMMO_DOMAIN.replace("https://", "").replace("http://", "")
    if host.endswith(".kommo.com"):
        AUTHORIZED_SUBDOMAIN = host.split(".kommo.com")[0]

if not OPENAI_API_KEY or not OPENAI_ASSISTANT_ID:
    raise RuntimeError("OPENAI_API_KEY ou OPENAI_ASSISTANT_ID não configurados.")

# Cliente OpenAI (Erika)
client = OpenAI(api_key=OPENAI_API_KEY)

app = FastAPI()


@app.get("/health")
async def health_check():
    return {"status": "ok"}


def call_openai_zidane(message: str) -> str:
    """
    Chama a Assistente (Erika/Zidane) na OpenAI usando o Assistants API.
    """

    try:
        # Cria um thread com a mensagem do cliente
        thread = client.beta.threads.create(
            messages=[
                {
                    "role": "user",
                    "content": message,
                }
            ]
        )

        # Executa a Assistente e espera terminar
        run = client.beta.threads.runs.create_and_poll(
            thread_id=thread.id,
            assistant_id=OPENAI_ASSISTANT_ID,
        )

        if run.status != "completed":
            return (
                "No momento não consegui concluir a resposta, "
                "pode repetir a mensagem ou tentar novamente em instantes?"
            )

        # Busca a última mensagem gerada pela assistente
        messages = client.beta.threads.messages.list(
            thread_id=thread.id, order="desc", limit=1
        )

        if not messages.data:
            return "Não consegui gerar uma resposta agora, pode repetir a mensagem por favor?"

        answer = ""
        for part in messages.data[0].content:
            if part.type == "text":
                answer += part.text.value

        answer = answer.strip()
        if not answer:
            return "Não consegui gerar uma resposta agora, pode repetir a mensagem por favor?"

        return answer

    except Exception as e:
        # Em caso de erro na OpenAI, devolve uma resposta segura para o cliente
        print("Erro ao chamar a OpenAI:", e)
        return (
            "Tive um problema técnico aqui do meu lado ao montar a resposta. "
            "Pode tentar de novo em alguns instantes, por favor?"
        )


@app.post("/kommo-webhook")
async def kommo_webhook(request: Request):
    """
    Endpoint de Webhook para o Kommo.
    Recebe mensagens, chama a IA e cria uma nota no lead (quando houver lead_id).
    """
    payload = await request.json()

    # Validação básica de subdomínio do Kommo (se configurado)
    account = payload.get("account") or {}
    subdomain = account.get("subdomain")

    if AUTHORIZED_SUBDOMAIN and subdomain and subdomain != AUTHORIZED_SUBDOMAIN:
        raise HTTPException(
            status_code=401,
            detail=f"Subdomínio não autorizado: {subdomain}",
        )

    # ==========================
    # Extrai mensagem e lead
    # ==========================
    data_section = payload.get("data") or {}

    message = (
        (payload.get("message") or {}).get("text")
        or payload.get("text")
        or (payload.get("last_message") or {}).get("text")
        or (data_section.get("message") or {}).get("text")
        or data_section.get("text")
        or (data_section.get("last_message") or {}).get("text")
        or ""
    )

    lead = (
        payload.get("lead")
        or data_section.get("lead")
        or {}
    )

    lead_id = (
        (lead or {}).get("id")
        or payload.get("lead_id")
        or data_section.get("lead_id")
    )

    if not message:
        # Nada para processar, apenas retorna status de ignorado
        return {
            "status": "ignored",
            "reason": "sem mensagem",
            "payload_keys": list(payload.keys()),
        }

    # ==========================
    # Chama a IA (Erika)
    # ==========================
    resposta = call_openai_zidane(message)

    # ==========================
    # Cria nota no Kommo (se houver lead_id)
    # ==========================
    note_status = "skipped_no_lead"

    if KOMMO_DOMAIN and KOMMO_TOKEN and lead_id:
        try:
            # A API do Kommo espera uma LISTA de notas
            note_data = [
                {
                    "entity_id": lead_id,
                    "note_type": "common",
                    "params": {
                        "text": f"💬 Erika: {resposta}",
                    },
                }
            ]

            r = requests.post(
                f"{KOMMO_DOMAIN}/api/v4/leads/notes",
                headers={"Authorization": f"Bearer {KOMMO_TOKEN}"},
                json=note_data,
                timeout=30,
            )
            r.raise_for_status()
            note_status = "ok"

        except Exception as e:
            # Não derruba o fluxo se a nota falhar, apenas retorna o erro junto
            return {
                "status": "ok",
                "lead_id": lead_id,
                "ai_response": resposta,
                "kommo_note": "failed",
                "error": str(e),
            }

    return {
        "status": "ok",
        "lead_id": lead_id,
        "ai_response": resposta,
        "kommo_note": note_status,
    }
