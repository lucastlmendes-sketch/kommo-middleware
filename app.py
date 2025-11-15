import os
import json
import requests
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from openai import OpenAI

# ============================================================
# CONFIGURAÇÕES
# ============================================================

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_ASSISTANT_ID = os.getenv("OPENAI_ASSISTANT_ID", "")  # Assistente da Erika
KOMMO_TOKEN = os.getenv("KOMMO_TOKEN", "")
KOMMO_DOMAIN = os.getenv("KOMMO_DOMAIN", "")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY não configurada.")

if not OPENAI_ASSISTANT_ID:
    raise RuntimeError("OPENAI_ASSISTANT_ID não configurada.")

if not KOMMO_TOKEN:
    raise RuntimeError("KOMMO_TOKEN não configurado.")

if not KOMMO_DOMAIN:
    raise RuntimeError("KOMMO_DOMAIN não configurado.")

client = OpenAI(api_key=OPENAI_API_KEY)

# Mapear etapas do funil (opcional)
STAGE_ENV_MAP = {
    "novo": "123456",
    "qualificacao": "654321"
}

# ============================================================
# FUNÇÕES DE APOIO
# ============================================================

def log(*args):
    print("[LOG]", *args, flush=True)


def add_kommo_note(lead_id: int, text: str):
    """
    Adiciona nota ao lead no Kommo
    """
    url = f"https://{KOMMO_DOMAIN}/api/v4/leads/{lead_id}/notes"
    headers = {"Authorization": f"Bearer {KOMMO_TOKEN}"}
    payload = {
        "note_type": "common",
        "params": {"text": text}
    }
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        log("Nota adicionada:", r.status_code, r.text[:200])
    except Exception as e:
        log("Erro ao adicionar nota:", repr(e))


def update_lead_stage(lead_id: int, stage_id: str):
    """
    Atualiza etapa do lead no Kommo
    """
    url = f"https://{KOMMO_DOMAIN}/api/v4/leads/{lead_id}"
    headers = {"Authorization": f"Bearer {KOMMO_TOKEN}"}

    payload = {"status_id": stage_id}

    try:
        r = requests.patch(url, json=payload, headers=headers, timeout=10)
        log("Mudança de etapa:", r.status_code, r.text[:200])
    except Exception as e:
        log("Erro ao atualizar etapa:", repr(e))


def extract_visible_and_action(text: str):
    """
    Divide a saída do assistant em:
    ---VISIBLE---
    (texto para cliente)
    ---ERIKA_ACTION---
    (estrutura JSON)
    """
    if "---VISIBLE---" in text and "---ERIKA_ACTION---" in text:
        parts = text.split("---VISIBLE---")[1].split("---ERIKA_ACTION---")
        visible = parts[0].strip()
        action_raw = parts[1].strip()

        try:
            action = json.loads(action_raw)
        except:
            log("Falha ao interpretar ERIKA_ACTION como json.")
            action = None

        return visible, action

    return text, None


def call_erika_assistant(message: str):
    """
    Chama o Assistant da Erika (OpenAI)
    """
    try:
        thread = client.beta.threads.create()
        client.beta.threads.messages.create(
            thread_id=thread.id,
            role="user",
            content=message,
        )

        run = client.beta.threads.runs.create_and_poll(
            thread_id=thread.id,
            assistant_id=OPENAI_ASSISTANT_ID,
        )

        if run.status != "completed":
            return "Desculpe, estou com dificuldades para responder agora. 😔"

        msgs = client.beta.threads.messages.list(thread_id=thread.id)
        text = "\n".join(
            c.text.value
            for m in msgs.data
            for c in m.content
            if hasattr(c, "text")
        )

        return text.strip()
    except Exception as e:
        log("Erro no assistant:", repr(e))
        return "Ops! Algo deu errado ao falar com a Erika."


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI()


@app.get("/")
def home():
    return {"status": "ok", "message": "Kommo middleware ativo."}


@app.post("/kommo-webhook")
async def kommo_webhook(request: Request):
    """
    RECEBE widget_request do Kommo
    Responde a Erika via OpenAI
    Envia devolutiva para return_url
    """
    raw = await request.body()
    content_type = request.headers.get("content-type", "").lower()

    try:
        if "json" in content_type:
            payload = json.loads(raw.decode("utf-8"))
        else:
            # fallback: form-urlencoded
            from urllib.parse import parse_qs
            payload = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
    except Exception as e:
        log("Erro ao interpretar payload:", repr(e))
        raise HTTPException(400, "Payload inválido")

    log("Payload recebido:", json.dumps(payload)[:800])

    # Estrutura típica:
    # {
    #   "token": "...",
    #   "data": { "message": "texto...", "from": "widget" },
    #   "return_url": "https://.../continue/... "
    # }
    token = payload.get("token")
    data = payload.get("data") or {}
    return_url = payload.get("return_url")

    # Extrair texto enviado pelo cliente
    msg_raw = data.get("message") or data.get("text") or ""

    if isinstance(msg_raw, dict):
        message_text = msg_raw.get("text") or msg_raw.get("body") or msg_raw.get("message", "")
    else:
        message_text = str(msg_raw)

    message_text = message_text.strip()

    if not message_text:
        log("Nenhuma mensagem encontrada.")
        return {"status": "ignored"}

    # Extrair lead_id caso exista
    lead_id = None
    if isinstance(data.get("lead"), dict):
        lead_id = data["lead"].get("id")
    if not lead_id:
        lead_id = data.get("lead_id")

    # ============================
    # CHAMAR ASSISTENTE DA ERIKA
    # ============================

    erika_raw = call_erika_assistant(message_text)

    visible, erika_action = extract_visible_and_action(erika_raw)

    if not visible:
        visible = "Ok! Recebi sua mensagem. 😊"

    # ======================================
    # INTERAÇÕES NO KOMMO (notas & pipeline)
    # ======================================

    if lead_id:
        add_kommo_note(lead_id, f"Erika 🧠:\n{visible}")

        if erika_action and isinstance(erika_action, dict):
            summary = erika_action.get("summary_note")
            if summary:
                add_kommo_note(lead_id, f"ERIKA_ACTION: {summary}")

            stage_key = erika_action.get("kommo_suggested_stage")
            if stage_key and stage_key in STAGE_ENV_MAP:
                update_lead_stage(lead_id, STAGE_ENV_MAP[stage_key])

    # ============================
    # CHAMAR RETURN_URL (OBRIGATÓRIO)
    # ============================

    if return_url:
        try:
            short_message = visible[:80]

            body = {
                "data": {"message": visible},
                "execute_handlers": [
                    {
                        "handler": "show",
                        "params": {"type": "text", "value": short_message}
                    }
                ]
            }

            log("POST -> return_url:", return_url)
            r = requests.post(return_url, json=body, timeout=10)
            log("Resposta return_url:", r.status_code, r.text[:300])

        except Exception as e:
            log("Erro ao chamar return_url:", repr(e))

    return {
        "status": "ok",
        "message_sent": visible,
        "lead_id": lead_id,
        "erika_action": erika_action
    }
